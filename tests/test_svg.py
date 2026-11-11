from __future__ import annotations

import importlib.util
from types import SimpleNamespace
from typing import Any

import discord
import pytest

from discord_bot.discord_app.chat import ChatService
from discord_bot.tools.base import GeneratedFile, ToolContext
from discord_bot.tools.svg import (
    MAX_PROMPT_CHARS,
    SvgError,
    build_tool,
    extract_svg,
    generate_svg,
    png_size,
    validate_svg,
)

VALID_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 50">'
    '<rect width="100" height="50" fill="#ffffff"/>'
    '<text x="10" y="30" font-family="sans-serif">Схема</text>'
    "</svg>"
)


def fake_png(width: int = 400, height: int = 200) -> bytes:
    return (
        b"\x89PNG\r\n\x1a\n"
        + b"\x00" * 8
        + width.to_bytes(4, "big")
        + height.to_bytes(4, "big")
        + b"\x00" * 16
    )


class FakeLLM:
    def __init__(self, outputs: list[str]) -> None:
        self.outputs = list(outputs)
        self.calls: list[dict[str, Any]] = []

    async def complete_text(
        self,
        provider: Any,
        model: str,
        messages: list[dict[str, Any]],
        **kwargs: Any,
    ) -> str:
        self.calls.append(
            {
                "provider": provider,
                "model": model,
                "messages": messages,
                "kwargs": kwargs,
            }
        )
        return self.outputs.pop(0)


def provider() -> SimpleNamespace:
    return SimpleNamespace(name="gemini", default_model="gemini-3.8-flash")


def context(llm: Any = None, **overrides: Any) -> ToolContext:
    values = {
        "user_id": "user-1",
        "channel_id": "chan-1",
        "settings": {"svg_cooldown_seconds": "0"},
        "llm": llm,
        "provider": provider(),
        "model": "gemini-3.5-flash-lite",
    }
    values.update(overrides)
    return ToolContext(**values)


def test_extract_svg_strips_fences_and_surrounding_text() -> None:
    raw = f"Вот рисунок:\n```svg\n{VALID_SVG}\n```\nГотово."

    assert extract_svg(raw) == VALID_SVG


def test_extract_svg_rejects_text_without_a_document() -> None:
    with pytest.raises(SvgError):
        extract_svg("просто текст")


def test_valid_svg_passes() -> None:
    validate_svg(VALID_SVG)


@pytest.mark.parametrize(
    "svg",
    [
        '<svg viewBox="0 0 10 10"><script>alert(1)</script></svg>',
        '<svg viewBox="0 0 10 10"><foreignObject></foreignObject></svg>',
        '<svg viewBox="0 0 10 10"><style>@import url(http://x)</style></svg>',
        '<svg viewBox="0 0 10 10"><image href="#x"/></svg>',
        '<svg viewBox="0 0 10 10"><rect width="1" height="1" onload="x()"/></svg>',
        '<svg viewBox="0 0 10 10"><rect fill="url(http://x)"/></svg>',
        '<svg viewBox="0 0 10 10"><a href="https://example.com">x</a></svg>',
        '<svg viewBox="0 0 10 10"><rect fill="javascript:alert(1)"/></svg>',
        '<svg viewBox="0 0 10 10"><link href="https://x/y.css"/></svg>',
        '<!DOCTYPE svg><svg viewBox="0 0 10 10"></svg>',
        '<svg viewBox="0 0 10 10" xml:base="https://example.com"></svg>',
        "<div>not svg</div>",
        '<svg width="10" height="10"></svg>',
    ],
)
def test_unsafe_or_invalid_svg_is_rejected(svg: str) -> None:
    with pytest.raises(SvgError):
        validate_svg(svg)


def test_too_many_elements_are_rejected() -> None:
    rects = '<rect width="1" height="1"/>' * 400
    svg = f'<svg viewBox="0 0 10 10">{rects}</svg>'

    with pytest.raises(SvgError):
        validate_svg(svg)


def test_png_size_reads_the_ihdr() -> None:
    assert png_size(fake_png(640, 480)) == (640, 480)


async def test_tool_renders_and_attaches_a_png() -> None:
    async def generate(description: str, ctx: ToolContext) -> str:
        return VALID_SVG

    tool = build_tool(generator=generate, renderer=lambda svg: fake_png())
    ctx = context()

    result = await tool.handler(ctx, "схема RAG")

    assert "PNG 400×200" in result
    assert "gemini-3.8-flash" in result
    assert len(ctx.files) == 1
    assert ctx.files[0].filename.endswith(".png")
    assert ctx.files[0].mime == "image/png"
    assert ctx.files[0].data.startswith(b"\x89PNG")


async def test_tool_attaches_the_source_only_on_request() -> None:
    async def generate(description: str, ctx: ToolContext) -> str:
        return VALID_SVG

    tool = build_tool(generator=generate, renderer=lambda svg: fake_png())
    ctx = context()

    result = await tool.handler(ctx, "схема", attach_svg=True)

    assert len(ctx.files) == 2
    assert ctx.files[1].filename.endswith(".svg")
    assert ctx.files[1].mime == "image/svg+xml"
    assert ctx.files[1].data.decode("utf-8") == VALID_SVG
    assert "Исходный SVG" in result


async def test_tool_rejects_a_too_long_prompt() -> None:
    async def generate(description: str, ctx: ToolContext) -> str:
        raise AssertionError("generator must not run")

    tool = build_tool(generator=generate, renderer=lambda svg: fake_png())

    result = await tool.handler(context(), "x" * (MAX_PROMPT_CHARS + 1))

    assert "длиннее" in result


async def test_tool_cooldown_is_per_user() -> None:
    now = [1000.0]

    async def generate(description: str, ctx: ToolContext) -> str:
        return VALID_SVG

    tool = build_tool(
        generator=generate,
        renderer=lambda svg: fake_png(),
        clock=lambda: now[0],
    )
    settings = {"svg_cooldown_seconds": "60"}

    first = await tool.handler(context(settings=settings), "первый")
    second = await tool.handler(context(settings=settings), "второй")
    now[0] += 61
    third = await tool.handler(context(settings=settings), "третий")

    assert "Готово" in first
    assert "Слишком часто" in second
    assert "Готово" in third


async def test_tool_respects_the_file_limit() -> None:
    async def generate(description: str, ctx: ToolContext) -> str:
        raise AssertionError("generator must not run")

    tool = build_tool(generator=generate, renderer=lambda svg: fake_png())
    ctx = context()
    ctx.files.extend(
        [
            GeneratedFile("a.png", b"x"),
            GeneratedFile("b.png", b"y"),
        ]
    )

    result = await tool.handler(ctx, "схема")

    assert "два файла" in result


async def test_generator_retries_after_a_broken_document() -> None:
    llm = FakeLLM(["не svg", VALID_SVG])
    ctx = context(llm=llm)

    svg = await generate_svg("схема", ctx)

    assert svg == VALID_SVG
    assert len(llm.calls) == 2
    assert "не прошёл проверку" in llm.calls[1]["messages"][1]["content"]


async def test_generator_gives_up_after_two_broken_documents() -> None:
    llm = FakeLLM(["не svg", "опять не svg"])

    with pytest.raises(SvgError):
        await generate_svg("схема", context(llm=llm))


async def test_generator_without_llm_context_fails_cleanly() -> None:
    with pytest.raises(SvgError):
        await generate_svg("схема", context(llm=None))


def test_tool_is_registered_with_a_long_timeout() -> None:
    tool = build_tool(
        generator=lambda description, ctx: None,
        renderer=lambda svg: fake_png(),
    )

    assert tool.name == "draw_svg"
    assert tool.timeout == 75.0


@pytest.mark.skipif(
    importlib.util.find_spec("resvg_py") is None,
    reason="resvg_py is not installed",
)
async def test_real_resvg_renders_a_png() -> None:
    async def generate(description: str, ctx: ToolContext) -> str:
        return VALID_SVG

    tool = build_tool(generator=generate)
    ctx = context()

    result = await tool.handler(ctx, "схема")

    assert "PNG" in result
    assert ctx.files[0].data.startswith(b"\x89PNG")
    assert png_size(ctx.files[0].data)[0] == 1200


# --- sending attachments ----------------------------------------------------


class FakeMessage:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.channel = SimpleNamespace(
            send=self._send,
            id=1,
        )

    async def _send(self, *args: Any, **kwargs: Any) -> None:
        self.calls.append({"args": args, "kwargs": kwargs})

    async def reply(self, *args: Any, **kwargs: Any) -> None:
        self.calls.append({"args": args, "kwargs": kwargs})


def chat_service() -> ChatService:
    return ChatService(None, None, None, None, None)  # type: ignore[arg-type]


async def test_send_attaches_files_to_the_first_message() -> None:
    chat = chat_service()
    message = FakeMessage()
    files = [GeneratedFile("drawing.png", fake_png(), "image/png")]

    await chat._send(message, "Готово", 1900, files=files)

    assert len(message.calls) == 1
    sent = message.calls[0]["kwargs"]["files"]
    assert len(sent) == 1
    assert sent[0].filename == "drawing.png"


class RejectedFiles(discord.HTTPException):
    status = 413
    code = 0
    text = "Request Entity Too Large"

    def __init__(self) -> None:
        pass


async def test_send_repeats_without_files_when_discord_rejects_them() -> None:
    chat = chat_service()
    message = FakeMessage()
    original_reply = message.reply
    calls = [0]

    async def reply(*args: Any, **kwargs: Any) -> None:
        calls[0] += 1
        if calls[0] == 1:
            raise RejectedFiles()
        await original_reply(*args, **kwargs)

    message.reply = reply  # type: ignore[method-assign]
    files = [GeneratedFile("drawing.png", fake_png(), "image/png")]

    await chat._send(message, "Готово", 1900, files=files)

    assert len(message.calls) == 1
    assert "files" not in message.calls[0]["kwargs"]
