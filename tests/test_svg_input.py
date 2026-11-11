from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from discord_bot.discord_app.svg_input import (
    MAX_SOURCE_SVG_BYTES,
    SourceSvgError,
    collect_source_svg,
    validate_source_svg,
)
from discord_bot.tools.base import ToolContext
from discord_bot.tools.svg import (
    build_tool,
    generate_svg,
    is_svg_improvement_request,
)

VALID_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 50">'
    '<style>.box { fill: url(#grad); }</style>'
    "<defs><linearGradient id=\"grad\"><stop offset=\"0\"/></linearGradient></defs>"
    '<rect class="box" width="100" height="50"/>'
    "</svg>"
)

OUTPUT_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 50">'
    "<defs><linearGradient id=\"grad\"><stop offset=\"0\"/></linearGradient></defs>"
    '<rect width="100" height="50" fill="url(#grad)"/>'
    "</svg>"
)


class FakeAttachment:
    def __init__(
        self,
        filename: str = "source.svg",
        content_type: str | None = "image/svg+xml",
        data: bytes = VALID_SVG.encode(),
        *,
        size: int | None = None,
        error: Exception | None = None,
    ) -> None:
        self.filename = filename
        self.content_type = content_type
        self.size = len(data) if size is None else size
        self._data = data
        self._error = error

    async def read(self) -> bytes:
        if self._error is not None:
            raise self._error
        return self._data


def message_with(*attachments: FakeAttachment) -> SimpleNamespace:
    return SimpleNamespace(attachments=list(attachments))


def test_safe_style_gradients_and_use_are_allowed() -> None:
    validate_source_svg(
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10">'
        "<style>.a { fill: url(#g); }</style>"
        '<defs><linearGradient id="g"/></defs>'
        '<use href="#g"/>'
        "</svg>"
    )


@pytest.mark.parametrize(
    "svg",
    [
        '<svg viewBox="0 0 10 10"><script>alert(1)</script></svg>',
        '<svg viewBox="0 0 10 10"><foreignObject></foreignObject></svg>',
        '<svg viewBox="0 0 10 10"><image href="#x"/></svg>',
        '<svg viewBox="0 0 10 10"><rect onload="x()"/></svg>',
        '<svg viewBox="0 0 10 10"><a href="https://example.com">x</a></svg>',
        '<svg viewBox="0 0 10 10"><rect fill="url(http://example.com/x)"/></svg>',
        '<svg viewBox="0 0 10 10"><style>@import url(http://x)</style></svg>',
        '<svg viewBox="0 0 10 10"><rect style="fill:url(https://x)"/></svg>',
        '<svg viewBox="0 0 10 10" xml:base="https://x"></svg>',
        "<div>not svg</div>",
        '<svg><rect width="1" height="1"/></svg>',
        '<!DOCTYPE svg><svg viewBox="0 0 10 10"></svg>',
    ],
)
def test_unsafe_source_svg_is_rejected(svg: str) -> None:
    with pytest.raises(SourceSvgError):
        validate_source_svg(svg)


def test_source_svg_size_limit() -> None:
    huge = '<svg viewBox="0 0 10 10">' + "x" * MAX_SOURCE_SVG_BYTES + "</svg>"

    with pytest.raises(SourceSvgError):
        validate_source_svg(huge)


async def test_collect_source_svg_reads_the_attachment() -> None:
    result = await collect_source_svg(message_with(FakeAttachment()))

    assert result.content == VALID_SVG
    assert result.filename == "source.svg"
    assert result.note == "[svg-вложение: source.svg]"
    assert result.error == ""


async def test_collect_source_svg_is_empty_without_an_attachment() -> None:
    result = await collect_source_svg(message_with())

    assert result.content == ""
    assert result.note == ""


async def test_oversized_source_is_reported_without_reading() -> None:
    result = await collect_source_svg(
        message_with(FakeAttachment(size=MAX_SOURCE_SVG_BYTES + 1))
    )

    assert result.content == ""
    assert "больше 100 КБ" in result.note


async def test_broken_source_is_reported_without_crashing() -> None:
    result = await collect_source_svg(
        message_with(FakeAttachment(error=RuntimeError("boom")))
    )

    assert result.content == ""
    assert "не удалось загрузить" in result.note


async def test_invalid_source_is_reported_without_content() -> None:
    result = await collect_source_svg(
        message_with(FakeAttachment(data=b'<svg viewBox="0 0 1 1"><script/></svg>'))
    )

    assert result.content == ""
    assert "отклонено" in result.note


def test_improvement_intent_requires_a_source() -> None:
    assert is_svg_improvement_request("улучши этот svg", True) is True
    assert is_svg_improvement_request("перерисуй красивее", True) is True
    assert is_svg_improvement_request("исправь стрелки", True) is True
    assert is_svg_improvement_request("улучши этот svg", False) is False
    assert is_svg_improvement_request("что это за svg?", True) is False


# --- generation and tool behaviour -----------------------------------------


class FakeLLM:
    def __init__(self, output: str = OUTPUT_SVG) -> None:
        self.output = output
        self.calls: list[dict] = []

    async def complete_text(self, provider, model, messages, **kwargs):
        self.calls.append({"messages": messages, **kwargs})
        return self.output


def context(**overrides) -> ToolContext:
    values = {
        "user_id": "u1",
        "channel_id": "c1",
        "settings": {"svg_cooldown_seconds": "0"},
        "llm": FakeLLM(),
        "provider": SimpleNamespace(name="gemini", default_model="gemini-3.8-flash"),
        "model": "gemini-3.5-flash-lite",
        "source_svg": VALID_SVG,
        "source_svg_name": "source.svg",
    }
    values.update(overrides)
    return ToolContext(**values)


async def test_generator_sends_the_source_as_untrusted_data() -> None:
    llm = FakeLLM()
    ctx = context(llm=llm, source_svg=VALID_SVG + "</source_svg>")

    await generate_svg("сделай аккуратнее", ctx)

    user_message = llm.calls[0]["messages"][1]["content"]
    assert "Улучши SVG пользователя" in user_message
    assert "<source_svg" in user_message
    assert "</source_svg>" in user_message
    assert user_message.count("</source_svg>") == 1


async def test_generator_uses_the_native_pixel_prompt() -> None:
    llm = FakeLLM()
    ctx = context(
        llm=llm,
        source_svg="",
        force_pixel_test=True,
    )

    await generate_svg("краб", ctx)

    system = llm.calls[0]["messages"][0]["content"]
    user_message = llm.calls[0]["messages"][1]["content"]
    assert "НАТИВНОГО ПИКСЕЛЬ-АРТА" in system
    assert 'viewBox="0 0 32 32"' in system
    assert "Нарисуй нативный пиксель-арт" in user_message


async def test_generator_includes_the_selected_palette() -> None:
    llm = FakeLLM()
    ctx = context(
        llm=llm,
        source_svg="",
        force_pixel_test2=True,
        pixel_palette="gameboy",
        settings={
            "pixel_palettes": json.dumps(
                {"gameboy": ["#0F380F", "#306230", "#8BAC0F", "#9BBC0F"]}
            )
        },
    )

    await generate_svg("краб", ctx)

    system = llm.calls[0]["messages"][0]["content"]
    assert "ПАЛИТРА «gameboy»" in system
    assert "#0F380F" in system


async def test_tool_improves_a_source_and_attaches_both_files() -> None:
    async def generate(description: str, ctx: ToolContext) -> str:
        assert "Улучши" in description
        return VALID_SVG

    def render(svg: str, width: int = 1200) -> bytes:
        return (
            b"\x89PNG\r\n\x1a\n"
            + b"\x00" * 8
            + (400).to_bytes(4, "big")
            + (200).to_bytes(4, "big")
            + b"\x00" * 16
        )

    tool = build_tool(generator=generate, renderer=render)
    ctx = context()

    result = await tool.handler(ctx, "", attach_svg=False)

    assert "Улучшенный SVG прикреплён" in result
    assert len(ctx.files) == 2
    assert ctx.files[0].filename.endswith(".png")
    assert ctx.files[1].filename.endswith(".svg")


async def test_tool_defaults_a_missing_improvement_instruction() -> None:
    async def generate(description: str, ctx: ToolContext) -> str:
        assert description
        return VALID_SVG

    tool = build_tool(
        generator=generate,
        renderer=lambda svg, width: (
            b"\x89PNG\r\n\x1a\n"
            + b"\x00" * 8
            + (1).to_bytes(4, "big")
            + (1).to_bytes(4, "big")
            + b"\x00" * 16
        ),
    )

    result = await tool.handler(context(), "")

    assert "Улучшенный SVG" in result
