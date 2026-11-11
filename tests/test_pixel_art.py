from __future__ import annotations

import io
from types import SimpleNamespace

import pytest
from PIL import Image

from discord_bot.discord_app.pixel_art import PixelArtError, pixelate
from discord_bot.tools.base import ToolContext
from discord_bot.tools.svg import build_tool, wants_pixel_art


def sample_png(width: int = 200, height: int = 100) -> bytes:
    image = Image.new("RGB", (width, height), (255, 255, 255))
    half_w, half_h = width // 2, height // 2
    colors = {
        "red": (255, 0, 0),
        "green": (0, 255, 0),
        "blue": (0, 0, 255),
        "yellow": (255, 255, 0),
    }
    for x in range(width):
        for y in range(height):
            color = (
                colors["red"]
                if x < half_w and y < half_h
                else colors["green"]
                if x >= half_w and y < half_h
                else colors["blue"]
                if x < half_w
                else colors["yellow"]
            )
            image.putpixel((x, y), color)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def test_pixelate_produces_crisp_blocks_and_a_small_palette() -> None:
    result = pixelate(sample_png(), grid=32, colors=8)

    with Image.open(io.BytesIO(result)) as image:
        assert image.size == (128, 64)
        colors = image.convert("RGB").getcolors(maxcolors=1000)
        assert colors is not None
        assert len(colors) <= 8
        top_left = image.getpixel((0, 0))
        assert image.getpixel((10, 10)) == top_left
        assert image.getpixel((23, 23)) == top_left
        assert image.getpixel((63, 10)) == top_left
        assert image.getpixel((64, 10)) != top_left


def test_pixelate_handles_a_tall_image() -> None:
    result = pixelate(sample_png(100, 300), grid=32, colors=16)

    with Image.open(io.BytesIO(result)) as image:
        assert image.height == 128
        assert image.width <= 128


def test_default_square_is_128_by_128() -> None:
    result = pixelate(sample_png(100, 100))

    with Image.open(io.BytesIO(result)) as image:
        assert image.size == (128, 128)


def test_pixelate_rejects_non_png_data() -> None:
    with pytest.raises(PixelArtError):
        pixelate(b"not a png")


def test_pixel_intent_is_detected_in_natural_language() -> None:
    assert wants_pixel_art("нарисуй краба в пиксель-арт") is True
    assert wants_pixel_art("сделай pixel art") is True
    assert wants_pixel_art("пикселизируй") is True
    assert wants_pixel_art("нарисуй краба") is False


def test_pixelate_rejects_broken_png() -> None:
    with pytest.raises(PixelArtError):
        pixelate(b"\x89PNG\r\n\x1a\nbroken")


async def test_draw_tool_attaches_a_pixel_art_png() -> None:
    png = sample_png()

    async def generate(description: str, ctx: ToolContext) -> str:
        return (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10">'
            '<rect width="10" height="10"/></svg>'
        )

    tool = build_tool(generator=generate, renderer=lambda svg: png)
    context = ToolContext(
        user_id="u1",
        channel_id="c1",
        settings={"pixel_grid": "32", "pixel_colors": "8"},
        llm=SimpleNamespace(),
        provider=SimpleNamespace(name="gemini", default_model="x"),
        model="gemini-3.5-flash-lite",
    )

    result = await tool.handler(context, "краб", pixel_art=True)

    assert "Пиксель-арт" in result
    assert "PNG 128×64" in result
    assert "сетка 32 px" in result
    assert "блок 4 px" in result
    assert len(context.files) == 1
    assert context.files[0].filename.endswith("-pixel.png")
    assert context.files[0].data.startswith(b"\x89PNG")


async def test_force_pixel_art_from_the_marker_is_honoured() -> None:
    png = sample_png()

    async def generate(description: str, ctx: ToolContext) -> str:
        return (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10">'
            '<rect width="10" height="10"/></svg>'
        )

    tool = build_tool(generator=generate, renderer=lambda svg: png)
    context = ToolContext(
        user_id="u1",
        channel_id="c1",
        settings={"pixel_grid": "32", "pixel_colors": "8"},
        provider=SimpleNamespace(name="gemini", default_model="x"),
        model="gemini-3.5-flash-lite",
        force_pixel_art=True,
    )

    result = await tool.handler(context, "краб", pixel_art=False)

    assert "Пиксель-арт" in result
    assert "блок 4 px" in result
    assert len(context.files) == 1
    assert context.files[0].filename.endswith("-pixel.png")
