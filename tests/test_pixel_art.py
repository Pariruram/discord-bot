from __future__ import annotations

import io
import json
from types import SimpleNamespace

import pytest
from PIL import Image

from discord_bot.discord_app.pixel_art import PixelArtError, upscale_nearest
from discord_bot.tools.base import ToolContext
from discord_bot.tools.svg import build_tool, png_size


def sample_png(width: int = 32, height: int = 32) -> bytes:
    image = Image.new("RGB", (width, height), (255, 255, 255))
    colors = ((255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 0))
    for x in range(width):
        for y in range(height):
            image.putpixel(
                (x, y),
                colors[(x >= width // 2) + 2 * (y >= height // 2)],
            )
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def make_context(**overrides) -> ToolContext:
    values = {
        "user_id": "u1",
        "channel_id": "c1",
        "settings": {},
        "provider": SimpleNamespace(name="gemini", default_model="x"),
        "model": "gemini-3.5-flash-lite",
        "force_pixel_art": True,
    }
    values.update(overrides)
    return ToolContext(**values)


class Renderer:
    def __init__(self) -> None:
        self.widths: list[int] = []

    def __call__(self, svg: str, width: int = 1200) -> bytes:
        self.widths.append(width)
        size = width if width <= 128 else 32
        return sample_png(size, size)


async def generate(description: str, ctx: ToolContext) -> str:
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
        '<rect width="32" height="32"/></svg>'
    )


def test_upscale_nearest_keeps_edges() -> None:
    result = upscale_nearest(sample_png(32, 32), 4)

    with Image.open(io.BytesIO(result)) as image:
        assert image.size == (128, 128)
        assert image.getpixel((0, 0)) == image.getpixel((3, 3))
        assert image.getpixel((3, 0)) == image.getpixel((0, 0))


def test_upscale_nearest_rejects_broken_data() -> None:
    with pytest.raises(PixelArtError):
        upscale_nearest(b"\x89PNG\r\n\x1a\nbroken", 4)


async def test_pixel_defaults_to_32_and_publishes_128_first() -> None:
    renderer = Renderer()
    tool = build_tool(generator=generate, renderer=renderer)
    context = make_context()

    result = await tool.handler(context, "краб")

    assert renderer.widths == [32]
    assert "128×128 + 32×32" in result
    assert len(context.files) == 2
    assert context.files[0].filename.endswith("-128.png")
    assert context.files[1].filename.endswith("-32.png")
    assert png_size(context.files[0].data) == (128, 128)
    assert png_size(context.files[1].data) == (32, 32)


async def test_pixel_grid_from_settings_and_marker() -> None:
    renderer = Renderer()
    tool = build_tool(generator=generate, renderer=renderer)

    first = make_context(
        settings={"pixel_grid": "64", "svg_cooldown_seconds": "0"}
    )
    second = make_context(
        settings={"svg_cooldown_seconds": "0"}, force_pixel_grid=16
    )

    await tool.handler(first, "краб")
    await tool.handler(second, "краб")

    assert renderer.widths == [64, 16]
    assert png_size(first.files[0].data) == (128, 128)
    assert png_size(first.files[1].data) == (64, 64)
    assert png_size(second.files[1].data) == (16, 16)


async def test_pixel_64_returns_128_and_64() -> None:
    renderer = Renderer()
    tool = build_tool(generator=generate, renderer=renderer)
    context = make_context(force_pixel_grid=64)

    result = await tool.handler(context, "краб")

    assert renderer.widths == [64]
    assert "128×128 + 64×64" in result
    assert png_size(context.files[0].data) == (128, 128)
    assert png_size(context.files[1].data) == (64, 64)


async def test_invalid_grid_is_rejected_without_render() -> None:
    renderer = Renderer()
    tool = build_tool(generator=generate, renderer=renderer)
    context = make_context(force_pixel_grid=48)

    result = await tool.handler(context, "краб")

    assert "16, 32, 64 или 128" in result
    assert renderer.widths == []
    assert context.files == []


async def test_palette_is_applied_before_the_upscale() -> None:
    gameboy = ["#0F380F", "#306230", "#8BAC0F", "#9BBC0F"]
    renderer = Renderer()
    tool = build_tool(generator=generate, renderer=renderer)
    context = make_context(
        settings={
            "pixel_palette": "gameboy",
            "pixel_palettes": json.dumps({"gameboy": gameboy}),
        }
    )

    result = await tool.handler(context, "краб")

    assert "палитра gameboy" in result
    with Image.open(io.BytesIO(context.files[1].data)) as image:
        colors = image.convert("RGB").getcolors(maxcolors=1000)
    assert colors is not None
    allowed = {
        (15, 56, 15),
        (48, 98, 48),
        (139, 172, 15),
        (155, 188, 15),
    }
    assert {color for _, color in colors} <= allowed


async def test_normal_drawing_still_returns_one_png() -> None:
    renderer = Renderer()
    tool = build_tool(generator=generate, renderer=renderer)
    context = make_context(force_pixel_art=False)

    result = await tool.handler(context, "краб")

    assert renderer.widths == [1200]
    assert "Готово" in result
    assert len(context.files) == 1
    assert context.files[0].filename.endswith(".png")


async def test_pixel_with_svg_returns_three_files() -> None:
    renderer = Renderer()
    tool = build_tool(generator=generate, renderer=renderer)
    context = make_context()

    result = await tool.handler(context, "краб", attach_svg=True)

    assert "Исходный SVG прикреплён" in result
    assert len(context.files) == 3
    assert context.files[2].filename.endswith(".svg")
