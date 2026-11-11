"""Draw diagrams and illustrations as SVG, then rasterize them for Discord.

The main chat model does not emit SVG itself: this tool runs a dedicated model
call with an illustration-oriented system prompt and a larger output budget.
The result is validated as XML (scripts, external references and dangerous
elements are rejected), rendered with resvg and attached as a PNG. The source
SVG is attached only when the user explicitly asks for it.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from defusedxml import ElementTree as ET

from discord_bot.llm.client import LLMError
from discord_bot.tools.base import GeneratedFile, Tool, ToolContext
from discord_bot.utils import safe_int

logger = logging.getLogger(__name__)

MAX_SVG_BYTES = 100_000
MAX_SVG_ELEMENTS = 300
MAX_PNG_BYTES = 3 * 1024 * 1024
MAX_PROMPT_CHARS = 1500
RENDER_WIDTH = 1200
RENDER_TIMEOUT_SECONDS = 20.0
GENERATION_MAX_TOKENS = 4096
GENERATION_ATTEMPTS = 2
DEFAULT_COOLDOWN_SECONDS = 15
MAX_FILES_PER_TURN = 2

ASSETS_DIR = Path(__file__).resolve().parents[1] / "assets"
FONT_FILES = tuple(
    path
    for path in (
        ASSETS_DIR / "DejaVuSans.ttf",
        ASSETS_DIR / "DejaVuSans-Bold.ttf",
    )
    if path.exists()
)

DESCRIPTION = (
    "Рисует схему, диаграмму, график, иллюстрацию, иконку или логотип и "
    "прикрепляет готовый PNG к ответу. Вызывай сам, когда пользователь просит "
    "что-то нарисовать/показать визуально или когда без картинки ответ будет "
    "хуже: «нарисуй…», «покажи схему…», «сделай график…», «как это выглядит…». "
    "Не используй для фотографий и реалистичных изображений — это векторная "
    "графика. Параметр prompt — подробное описание: что изобразить, стиль, "
    "цвета, подписи, композиция. attach_svg=true только если пользователь явно "
    "просит SVG/вектор/исходник для редактирования."
)

SVG_MASTER_PROMPT = """Ты — SVG Master, профессиональный векторный иллюстратор и инженер SVG.
Твоя задача — по описанию создать законченный, аккуратный SVG-документ.

Верни ТОЛЬКО SVG: начиная с <svg и заканчивая </svg>. Без Markdown, ```,
пояснений и текста вокруг.

Обязательно:
- xmlns="http://www.w3.org/2000/svg" и viewBox (по умолчанию 0 0 1024 1024);
- фон, если он нужен для читаемости (прямоугольник на весь viewBox);
- только векторные элементы: path, rect, circle, ellipse, line, polyline,
  polygon, g, defs, linearGradient, radialGradient, clipPath, text;
- корректный XML: закрытые теги, кавычки, без DOCTYPE и внешних сущностей;
- компактность: не больше 120 элементов, без гигантских path-полотен.

Запрещено: script, style, foreignObject, image, iframe, object, embed,
audio, video, animation, обработчики on*, внешние ссылки, url(http…),
data: URI, @import, встроенные шрифты и внешние изображения.

Если просят схему, диаграмму, график или инфографику:
- чистая сетка и иерархия, минимум декоративного шума;
- не больше 12 блоков и 20 связей: если данных больше, упрости;
- подписи font-family="sans-serif", размер не меньше 20 при viewBox 1024;
- высокий контраст текста и фона, стрелки и связи читаемы;
- не выдумывай данные: используй только то, что есть в описании.

Если просят иллюстрацию, иконку или логотип:
- выразительный силуэт, ограниченная палитра, свет/тени уместны;
- логотип — лаконичный масштабируемый знак, иконка — чистый силуэт.

Уважай явно заданные размеры, цвета, стиль и соотношение сторон. Если запрос
неоднозначен, прими разумные художественные решения и всё равно нарисуй.
"""

_FORBIDDEN_TAGS = {
    "script",
    "style",
    "link",
    "base",
    "foreignobject",
    "iframe",
    "object",
    "embed",
    "audio",
    "video",
    "image",
    "feimage",
    "animate",
    "animatemotion",
    "animatetransform",
    "set",
}
_DRAW_ACTION_RE = re.compile(
    r"\b(нарисуй|нарисуйте|нарисовать|нарисуешь|draw)\b", re.IGNORECASE
)
_VISUAL_REQUEST_RE = re.compile(
    r"\b(покажи|покажите|сделай|сделайте|построй|постройте|изобрази|"
    r"изобразите|визуализируй|визуализируйте|пришли|пришлите)\b"
    r".{0,80}\b(схем\w*|диаграмм\w*|график\w*|иллюстрац\w*|рисун\w*|"
    r"иконк\w*|логотип\w*|инфографик\w*|svg|картин\w*|изображен\w*)",
    re.IGNORECASE | re.DOTALL,
)
_SVG_SOURCE_RE = re.compile(r"\bsvg\b|вектор|исходник", re.IGNORECASE)


def is_drawing_request(text: str) -> bool:
    """True when the user clearly asks for a picture, not an explanation."""

    value = text or ""
    return bool(
        _DRAW_ACTION_RE.search(value) or _VISUAL_REQUEST_RE.search(value)
    )


def wants_svg_source(text: str) -> bool:
    return bool(_SVG_SOURCE_RE.search(text or ""))
_URL_FUNC_RE = re.compile(r"url\(\s*(['\"]?)(.*?)\1\s*\)", re.IGNORECASE)


class SvgError(Exception):
    """A user-facing SVG generation or validation failure."""


def extract_svg(text: str) -> str:
    """Extract a single SVG document from model output."""

    text = (text or "").strip()
    text = re.sub(r"^```(?:svg|xml)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text)
    start = text.find("<svg")
    end = text.rfind("</svg>")
    if start < 0 or end < 0 or end <= start:
        raise SvgError("модель не вернула полный SVG-документ")
    return text[start : end + len("</svg>")].strip()


def _check_attributes(element: Any) -> None:
    for name, value in element.attrib.items():
        attribute = name.split("}")[-1].lower()
        raw = str(value or "").strip()
        lowered = raw.lower()

        if attribute.startswith("on"):
            raise SvgError("SVG содержит обработчик событий")
        if attribute == "base" and name.startswith(
            "{http://www.w3.org/XML/1998/namespace}"
        ):
            raise SvgError("xml:base в SVG запрещён")
        if attribute in {"href", "src"} or attribute.endswith(":href"):
            if not raw.startswith("#"):
                raise SvgError("внешние ссылки в SVG запрещены")
        if "javascript:" in lowered or "@import" in lowered or "expression(" in lowered:
            raise SvgError("в SVG обнаружен потенциально опасный код")
        for match in _URL_FUNC_RE.finditer(lowered):
            target = match.group(2).strip()
            if not target.startswith("#"):
                raise SvgError("внешние url() в SVG запрещены")


def validate_svg(svg: str) -> None:
    """Parse the document and reject anything unsafe for rendering."""

    if not svg or len(svg.encode("utf-8")) > MAX_SVG_BYTES:
        raise SvgError("SVG слишком большой")
    lowered = svg.lower()
    if "<!doctype" in lowered or "<!entity" in lowered:
        raise SvgError("DOCTYPE и сущности в SVG запрещены")
    try:
        root = ET.fromstring(svg)
    except Exception as exc:  # noqa: BLE001 - defusedxml raises several types
        raise SvgError(f"некорректный XML/SVG: {exc}") from exc

    if root.tag.split("}")[-1].lower() != "svg":
        raise SvgError("корневой элемент должен быть <svg>")
    if not str(root.attrib.get("viewBox") or "").strip():
        raise SvgError("в SVG отсутствует viewBox")

    elements = 0
    for element in root.iter():
        elements += 1
        if elements > MAX_SVG_ELEMENTS:
            raise SvgError("слишком сложный SVG (больше 300 элементов)")
        tag = element.tag.split("}")[-1].lower()
        if tag in _FORBIDDEN_TAGS:
            raise SvgError(f"запрещённый SVG-элемент: <{tag}>")
        _check_attributes(element)


def png_size(data: bytes) -> tuple[int, int]:
    if not data.startswith(b"\x89PNG\r\n\x1a\n") or len(data) < 24:
        raise SvgError("рендерер вернул не PNG")
    width = int.from_bytes(data[16:20], "big")
    height = int.from_bytes(data[20:24], "big")
    return width, height


def render_svg(svg: str, width: int = RENDER_WIDTH) -> bytes:
    """Rasterize validated SVG with resvg (no Cairo, no system libraries)."""

    import resvg_py

    options: dict[str, Any] = {
        "svg_string": svg,
        "width": width,
        "background": "#ffffff",
    }
    if FONT_FILES:
        # Bundled fonts make labels predictable on Render and in CI.
        options["font_files"] = [str(path) for path in FONT_FILES]
        options["font_family"] = "DejaVu Sans"
        options["sans_serif_family"] = "DejaVu Sans"
    png = resvg_py.svg_to_bytes(**options)
    return bytes(png)


def _choose_model(context: ToolContext) -> str:
    configured = str(context.settings.get("svg_model") or "").strip()
    if configured:
        return configured
    # The current chat model is already warm and fast; a heavy default model
    # can exceed the 60s request timeout on Render free.
    return context.model or str(getattr(context.provider, "default_model", "") or "")


async def generate_svg(description: str, context: ToolContext) -> str:
    """Ask the SVG model for a document; never called without an LLM."""

    llm = context.llm
    provider = context.provider
    if llm is None or provider is None:
        raise SvgError("генерация SVG недоступна: нет LLM-контекста")

    model = _choose_model(context)
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SVG_MASTER_PROMPT},
        {"role": "user", "content": f"Создай SVG по этому описанию:\n\n{description}"},
    ]

    last_error = ""
    for attempt in range(1, GENERATION_ATTEMPTS + 1):
        text = await llm.complete_text(
            provider,
            model,
            messages,
            temperature=0.6,
            max_tokens=GENERATION_MAX_TOKENS,
            max_attempts=1,
        )
        try:
            svg = extract_svg(text)
            validate_svg(svg)
            return svg
        except SvgError as exc:
            last_error = str(exc)
            logger.info(
                "SVG-попытка %s/%s не прошла проверку: %s",
                attempt,
                GENERATION_ATTEMPTS,
                last_error,
            )
            if attempt >= GENERATION_ATTEMPTS:
                break
            messages = [
                {"role": "system", "content": SVG_MASTER_PROMPT},
                {
                    "role": "user",
                    "content": (
                        f"Создай SVG по этому описанию:\n\n{description}\n\n"
                        f"Предыдущий ответ не прошёл проверку: {last_error}. "
                        "Верни исправленный SVG и ничего больше."
                    ),
                },
            ]

    raise SvgError(last_error or "модель не вернула корректный SVG")


Generator = Callable[[str, ToolContext], Awaitable[str]]
Renderer = Callable[[str], bytes]


def build_tool(
    generator: Generator | None = None,
    renderer: Renderer | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> Tool:
    generate = generator or generate_svg
    render = renderer or render_svg
    last_draw: dict[str, float] = {}

    async def draw_svg(
        context: ToolContext, prompt: str, attach_svg: bool = False
    ) -> str:
        description = (prompt or "").strip()
        if not description:
            return "Не удалось нарисовать: пустое описание."
        if len(description) > MAX_PROMPT_CHARS:
            return (
                f"Не удалось нарисовать: описание длиннее "
                f"{MAX_PROMPT_CHARS} символов."
            )
        attach_svg = bool(attach_svg or context.force_svg_source)
        if len(context.files) >= MAX_FILES_PER_TURN:
            return "Не удалось нарисовать: к ответу уже прикреплено два файла."
        needed = 2 if attach_svg else 1
        if len(context.files) + needed > MAX_FILES_PER_TURN:
            return (
                "Не удалось нарисовать: для SVG-исходника не хватает места "
                "в лимите файлов."
            )

        cooldown = safe_int(
            context.settings.get("svg_cooldown_seconds"),
            DEFAULT_COOLDOWN_SECONDS,
            minimum=0,
            maximum=600,
        )
        now = clock()
        previous = last_draw.get(context.user_id)
        if cooldown > 0 and previous is not None and now - previous < cooldown:
            wait = int(cooldown - (now - previous)) + 1
            return f"Слишком часто: подожди {wait} сек перед новым рисунком."
        last_draw[context.user_id] = now

        try:
            svg = await generate(description, context)
        except SvgError as exc:
            return f"Не удалось нарисовать: {exc}."
        except LLMError as exc:
            logger.warning("SVG-модель недоступна: %s", exc)
            return (
                "Не удалось нарисовать: модель рисования не ответила. "
                "Попробуй ещё раз или выбери другую `svg_model`."
            )
        except Exception:  # noqa: BLE001 - drawing must never break the chat
            logger.exception("Ошибка генерации SVG")
            return "Не удалось нарисовать: ошибка модели."

        try:
            png = await asyncio.wait_for(
                asyncio.to_thread(render, svg),
                timeout=RENDER_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            return "Не удалось нарисовать: рендер занял слишком много времени."
        except Exception as exc:  # noqa: BLE001 - broken SVG is user-facing
            logger.warning("Не удалось отрисовать SVG: %s", exc)
            return f"Не удалось нарисовать: SVG не отрисовался ({exc})."

        if len(png) > MAX_PNG_BYTES:
            return "Не удалось нарисовать: PNG получился больше 3 МБ."
        try:
            width, height = png_size(png)
        except SvgError as exc:
            return f"Не удалось нарисовать: {exc}."

        stem = f"drawing-{uuid.uuid4().hex[:8]}"
        context.files.append(
            GeneratedFile(f"{stem}.png", png, mime="image/png")
        )
        if attach_svg:
            context.files.append(
                GeneratedFile(
                    f"{stem}.svg",
                    svg.encode("utf-8"),
                    mime="image/svg+xml",
                )
            )

        model = _choose_model(context)
        suffix = " Исходный SVG прикреплён." if attach_svg else ""
        return (
            f"Готово: PNG {width}×{height}, {max(1, len(png) // 1024)} КБ, "
            f"модель {model}. Файл прикреплён к ответу.{suffix}"
        )

    return Tool(
        name="draw_svg",
        description=DESCRIPTION,
        parameters={
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": (
                        "Подробное описание рисунка: что изобразить, стиль, "
                        "цвета, подписи, расположение. Для схем перечисли "
                        "блоки и связи."
                    ),
                },
                "attach_svg": {
                    "type": "boolean",
                    "description": (
                        "true только если пользователь явно просит SVG, "
                        "векторный исходник или файл для редактирования."
                    ),
                    "default": False,
                },
            },
            "required": ["prompt"],
        },
        handler=draw_svg,
        timeout=75.0,
    )


def create_tool() -> Tool:
    return build_tool()
