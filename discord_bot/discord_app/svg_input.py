"""Read user-supplied SVG attachments as untrusted source material.

The source is never rendered directly: it only goes to the SVG model as data,
and the model's answer is validated and rendered by the normal pipeline.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Any

from defusedxml import ElementTree as ET

logger = logging.getLogger(__name__)

MAX_SOURCE_SVG_BYTES = 100_000
MAX_SOURCE_ELEMENTS = 500
DOWNLOAD_TIMEOUT_SECONDS = 15.0

_FORBIDDEN_TAGS = {
    "script",
    "foreignobject",
    "iframe",
    "object",
    "embed",
    "audio",
    "video",
    "image",
    "feimage",
    "link",
    "base",
    "animate",
    "animatemotion",
    "animatetransform",
    "set",
}
_URL_FUNC_RE = re.compile(r"url\(\s*(['\"]?)(.*?)\1\s*\)", re.IGNORECASE)


class SourceSvgError(Exception):
    """A user-facing problem with the supplied SVG."""


@dataclass(frozen=True, slots=True)
class SourceSvg:
    content: str = ""
    filename: str = ""
    note: str = ""
    error: str = ""


def _css_problem(value: str) -> str:
    lowered = value.lower()
    if "@import" in lowered:
        return "CSS @import"
    if "javascript:" in lowered:
        return "javascript:"
    if "expression(" in lowered:
        return "CSS expression()"
    for match in _URL_FUNC_RE.finditer(lowered):
        target = match.group(2).strip()
        if not target.startswith("#"):
            return "внешний url()"
    return ""


def _check_attributes(element: Any) -> None:
    for name, value in element.attrib.items():
        attribute = name.split("}")[-1].lower()
        raw = str(value or "").strip()
        if attribute.startswith("on"):
            raise SourceSvgError("обработчик событий")
        if attribute == "base" and name.startswith(
            "{http://www.w3.org/XML/1998/namespace}"
        ):
            raise SourceSvgError("xml:base")
        if attribute in {"href", "src"} or attribute.endswith(":href"):
            if not raw.startswith("#"):
                raise SourceSvgError("внешняя ссылка")
        problem = _css_problem(raw)
        if problem:
            raise SourceSvgError(problem)


def validate_source_svg(svg: str) -> None:
    """Validate an uploaded SVG before it is shown to the model."""

    if not svg.strip():
        raise SourceSvgError("пустой SVG")
    if len(svg.encode("utf-8-sig")) > MAX_SOURCE_SVG_BYTES:
        raise SourceSvgError("файл больше 100 КБ")
    lowered = svg.lower()
    if "<!doctype" in lowered or "<!entity" in lowered:
        raise SourceSvgError("DOCTYPE и сущности запрещены")
    try:
        root = ET.fromstring(svg)
    except Exception as exc:  # noqa: BLE001 - defusedxml raises several types
        raise SourceSvgError(f"некорректный XML: {exc}") from exc

    if root.tag.split("}")[-1].lower() != "svg":
        raise SourceSvgError("корневой элемент должен быть <svg>")
    if not any(
        str(root.attrib.get(name) or "").strip()
        for name in ("viewBox", "width", "height")
    ):
        raise SourceSvgError("в SVG нет viewBox или размеров")

    elements = 0
    for element in root.iter():
        elements += 1
        if elements > MAX_SOURCE_ELEMENTS:
            raise SourceSvgError("слишком сложный SVG (больше 500 элементов)")
        tag = element.tag.split("}")[-1].lower()
        if tag in _FORBIDDEN_TAGS:
            raise SourceSvgError(f"запрещённый элемент <{tag}>")
        _check_attributes(element)
        if tag == "style":
            problem = _css_problem("".join(element.itertext()))
            if problem:
                raise SourceSvgError(problem)


def _is_svg_attachment(attachment: Any) -> bool:
    filename = str(getattr(attachment, "filename", "") or "").lower()
    if filename.endswith(".svg"):
        return True
    content_type = str(getattr(attachment, "content_type", "") or "").lower()
    return content_type in {"image/svg+xml", "text/svg+xml"}


def _decode(data: bytes) -> str:
    for encoding in ("utf-8-sig", "cp1251"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise SourceSvgError("не удалось прочитать текст SVG")


async def collect_source_svg(message: Any) -> SourceSvg:
    """Read and validate the first SVG attachment on the message."""

    attachments = list(getattr(message, "attachments", []) or [])
    attachment = next(
        (item for item in attachments if _is_svg_attachment(item)), None
    )
    if attachment is None:
        return SourceSvg()

    filename = str(getattr(attachment, "filename", "") or "image.svg")
    size = int(getattr(attachment, "size", 0) or 0)
    if size > MAX_SOURCE_SVG_BYTES:
        return SourceSvg(
            filename=filename,
            note=f"[svg-вложение {filename} отклонено: файл больше 100 КБ]",
            error="файл больше 100 КБ",
        )

    try:
        data = await asyncio.wait_for(
            attachment.read(), timeout=DOWNLOAD_TIMEOUT_SECONDS
        )
    except Exception:  # noqa: BLE001 - a bad attachment must not break the reply
        logger.warning("Не удалось загрузить SVG %s", filename)
        return SourceSvg(
            filename=filename,
            note=f"[svg-вложение {filename} отклонено: не удалось загрузить]",
            error="не удалось загрузить",
        )

    if not data:
        return SourceSvg(
            filename=filename,
            note=f"[svg-вложение {filename} отклонено: пустой файл]",
            error="пустой файл",
        )
    if len(data) > MAX_SOURCE_SVG_BYTES:
        return SourceSvg(
            filename=filename,
            note=f"[svg-вложение {filename} отклонено: файл больше 100 КБ]",
            error="файл больше 100 КБ",
        )

    try:
        content = _decode(data)
        validate_source_svg(content)
    except SourceSvgError as exc:
        logger.info("SVG-вложение %s отклонено: %s", filename, exc)
        return SourceSvg(
            filename=filename,
            note=f"[svg-вложение {filename} отклонено: {exc}]",
            error=str(exc),
        )

    return SourceSvg(
        content=content,
        filename=filename,
        note=f"[svg-вложение: {filename}]",
    )
