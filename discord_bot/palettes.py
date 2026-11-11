"""Palette presets stored in bot_settings under ``pixel_palettes``."""

from __future__ import annotations

import io
import json
import logging
import re
from collections.abc import Mapping, Sequence
from typing import Any

from PIL import Image, UnidentifiedImageError

logger = logging.getLogger(__name__)

PALETTES_KEY = "pixel_palettes"
PALETTE_SETTING = "pixel_palette"
AUTO = "auto"
NAME_RE = re.compile(r"^[a-z0-9_-]{1,32}$")
HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
MIN_COLORS = 2
MAX_COLORS = 32

DEFAULT_PALETTES: dict[str, tuple[str, ...]] = {
    "pico8": (
        "#000000", "#1D2B53", "#7E2553", "#008751",
        "#AB5236", "#5F574F", "#C2C3C7", "#FFF1E8",
        "#FF004D", "#FFA300", "#FFEC27", "#00E436",
        "#29ADFF", "#83769C", "#FF77A8", "#FFCCAA",
    ),
    "gameboy": ("#0F380F", "#306230", "#8BAC0F", "#9BBC0F"),
    "sweetie16": (
        "#1A1C2C", "#5D275D", "#B13E53", "#EF7D57",
        "#FFCD75", "#A7F070", "#38B764", "#257179",
        "#29366F", "#3B5DC9", "#41A6F6", "#73EFF7",
        "#F4F4F4", "#94B0C2", "#566C86", "#333C57",
    ),
    "cga": ("#000000", "#55FFFF", "#FF55FF", "#FFFFFF"),
    "mono": ("#000000", "#FFFFFF"),
    "sepia": ("#2B1D0E", "#5B4636", "#A68A64", "#D9C7A7", "#F4ECD8"),
}


class PaletteError(ValueError):
    """A user-facing palette validation error."""


def normalize_name(name: str) -> str:
    value = str(name or "").strip().lower()
    if not NAME_RE.fullmatch(value):
        raise PaletteError(
            "имя палитры: 1–32 символа, только a-z, 0-9, _ и -"
        )
    return value


def normalize_colors(colors: Sequence[str]) -> tuple[str, ...]:
    normalized: list[str] = []
    for color in colors:
        value = str(color).strip().strip(",").upper()
        if not HEX_RE.fullmatch(value):
            raise PaletteError(f"цвет {color!r} должен быть в формате #RRGGBB")
        normalized.append(value)
    if not MIN_COLORS <= len(normalized) <= MAX_COLORS:
        raise PaletteError(f"нужно от {MIN_COLORS} до {MAX_COLORS} цветов")
    return tuple(normalized)


def _parse_raw(raw: str | None) -> dict[str, tuple[str, ...]] | None:
    if raw is None or not str(raw).strip():
        return None
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    palettes: dict[str, tuple[str, ...]] = {}
    for name, colors in data.items():
        try:
            normalized_name = normalize_name(str(name))
            if not isinstance(colors, list):
                continue
            palettes[normalized_name] = normalize_colors(
                [str(color) for color in colors]
            )
        except PaletteError:
            continue
    return palettes


def palettes_from_mapping(
    settings: Mapping[str, str],
) -> dict[str, tuple[str, ...]]:
    parsed = _parse_raw(settings.get(PALETTES_KEY))
    return dict(DEFAULT_PALETTES) if parsed is None else parsed


async def load_palettes(settings: Any) -> dict[str, tuple[str, ...]]:
    raw = await settings.get(PALETTES_KEY, "")
    parsed = _parse_raw(raw)
    return dict(DEFAULT_PALETTES) if parsed is None else parsed


async def save_palettes(
    settings: Any, palettes: Mapping[str, Sequence[str]]
) -> None:
    payload = {
        name: list(colors) for name, colors in palettes.items()
    }
    await settings.set(
        PALETTES_KEY, json.dumps(payload, ensure_ascii=False)
    )


async def ensure_palettes(settings: Any) -> None:
    """Seed defaults on first use; an explicit empty dict is respected."""

    try:
        raw = await settings.get(PALETTES_KEY, "")
        if str(raw).strip():
            return
        await save_palettes(settings, DEFAULT_PALETTES)
        logger.info("Записаны встроенные палитры: %s", ", ".join(DEFAULT_PALETTES))
    except Exception:  # noqa: BLE001 - palette seeding is not critical
        logger.exception("Не удалось записать встроенные палитры")


def _hex_to_rgb(value: str) -> tuple[int, int, int]:
    return (
        int(value[1:3], 16),
        int(value[3:5], 16),
        int(value[5:7], 16),
    )


def apply_palette(png: bytes, colors: Sequence[str]) -> bytes:
    """Map every pixel to the nearest color of the palette."""

    if not colors:
        return png
    try:
        with Image.open(io.BytesIO(png)) as source:
            rgba = source.convert("RGBA")
            background = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
            rgb = Image.alpha_composite(background, rgba).convert("RGB")
            palette = Image.new("P", (1, 1))
            flat: list[int] = []
            for color in colors:
                flat.extend(_hex_to_rgb(color))
            flat.extend([0] * (768 - len(flat)))
            palette.putpalette(flat)
            mapped = rgb.quantize(
                palette=palette, dither=Image.Dither.NONE
            ).convert("RGB")
            buffer = io.BytesIO()
            mapped.save(buffer, format="PNG", optimize=True)
            return buffer.getvalue()
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise PaletteError(f"не удалось применить палитру: {exc}") from exc
