from __future__ import annotations

import io
import json

import pytest
from PIL import Image

from discord_bot.palettes import (
    DEFAULT_PALETTES,
    PALETTES_KEY,
    PaletteError,
    apply_palette,
    ensure_palettes,
    load_palettes,
    normalize_colors,
    normalize_name,
    palettes_from_mapping,
    save_palettes,
)


class FakeSettings:
    def __init__(self, data: dict[str, str] | None = None) -> None:
        self.data = dict(data or {})
        self.writes: list[tuple[str, str]] = []

    async def get(self, key: str, default: str = "") -> str:
        return self.data.get(key, default)

    async def set(self, key: str, value: str) -> None:
        self.data[key] = value
        self.writes.append((key, value))


def png_with_colors(colors: list[tuple[int, int, int]]) -> bytes:
    image = Image.new("RGB", (len(colors), 1))
    for index, color in enumerate(colors):
        image.putpixel((index, 0), color)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def test_name_and_color_validation() -> None:
    assert normalize_name(" GameBoy ") == "gameboy"
    assert normalize_colors(["#0f380f", "#306230"]) == (
        "#0F380F",
        "#306230",
    )
    with pytest.raises(PaletteError):
        normalize_name("bad name!")
    with pytest.raises(PaletteError):
        normalize_colors(["red", "#000000"])
    with pytest.raises(PaletteError):
        normalize_colors(["#000000"])


def test_defaults_are_used_when_the_key_is_missing() -> None:
    assert palettes_from_mapping({}) == DEFAULT_PALETTES


def test_an_explicit_empty_dict_is_respected() -> None:
    assert palettes_from_mapping({PALETTES_KEY: "{}"}) == {}


async def test_ensure_seeds_defaults_once_and_respects_empty() -> None:
    settings = FakeSettings()
    await ensure_palettes(settings)
    assert PALETTES_KEY in settings.data
    assert json.loads(settings.data[PALETTES_KEY])["gameboy"]

    settings.data[PALETTES_KEY] = "{}"
    await ensure_palettes(settings)
    assert settings.data[PALETTES_KEY] == "{}"


async def test_load_and_save_palettes() -> None:
    settings = FakeSettings()
    await save_palettes(settings, {"mine": ("#000000", "#FFFFFF")})

    loaded = await load_palettes(settings)

    assert loaded["mine"] == ("#000000", "#FFFFFF")
    assert "gameboy" not in loaded


def test_apply_palette_maps_each_pixel_to_an_allowed_color() -> None:
    png = png_with_colors(
        [(250, 10, 10), (10, 250, 10), (10, 10, 250), (255, 255, 255)]
    )
    colors = ("#000000", "#FF0000", "#00FF00", "#0000FF")

    mapped = apply_palette(png, colors)

    with Image.open(io.BytesIO(mapped)) as image:
        actual = {image.getpixel((x, 0)) for x in range(4)}
    expected = {
        (0, 0, 0),
        (255, 0, 0),
        (0, 255, 0),
        (0, 0, 255),
    }
    assert actual <= expected
