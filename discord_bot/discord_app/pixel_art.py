"""Turn a rendered PNG into crisp pixel art with Palette + nearest-neighbor."""

from __future__ import annotations

import io
import logging

from PIL import Image, UnidentifiedImageError

logger = logging.getLogger(__name__)

DEFAULT_GRID = 64
DEFAULT_COLORS = 16
MIN_GRID = 32
MAX_GRID = 128
MIN_COLORS = 8
MAX_COLORS = 32
MAX_OUTPUT_SIDE = 768
MAX_OUTPUT_BYTES = 3 * 1024 * 1024


class PixelArtError(Exception):
    """A user-facing pixel-art conversion failure."""


def pixelate(
    png: bytes,
    *,
    grid: int = DEFAULT_GRID,
    colors: int = DEFAULT_COLORS,
) -> bytes:
    """Downscale, quantize and nearest-upscale a PNG.

    ``grid`` is the target width in pixels; the height follows the aspect
    ratio. The result is upscaled to at most 768 px so Discord shows crisp
    square blocks instead of a tiny smoothed image.
    """

    if not png.startswith(b"\x89PNG\r\n\x1a\n"):
        raise PixelArtError("на входе не PNG")
    grid = max(MIN_GRID, min(MAX_GRID, grid))
    colors = max(MIN_COLORS, min(MAX_COLORS, colors))

    try:
        with Image.open(io.BytesIO(png)) as source:
            rgba = source.convert("RGBA")
            if rgba.width <= 0 or rgba.height <= 0:
                raise PixelArtError("пустое изображение")
            background = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
            rgb = Image.alpha_composite(background, rgba).convert("RGB")

            target_w = grid
            target_h = max(1, round(rgb.height * target_w / rgb.width))
            if target_h > MAX_GRID:
                scale = MAX_GRID / target_h
                target_w = max(1, round(target_w * scale))
                target_h = MAX_GRID

            small = rgb.resize(
                (target_w, target_h), Image.Resampling.BOX
            )
            quantized = small.quantize(
                colors=colors,
                method=Image.Quantize.MEDIANCUT,
                dither=Image.Dither.NONE,
            ).convert("RGB")

            factor = max(1, MAX_OUTPUT_SIDE // max(target_w, target_h))
            output = quantized.resize(
                (target_w * factor, target_h * factor),
                Image.Resampling.NEAREST,
            )
            buffer = io.BytesIO()
            output.save(buffer, format="PNG", optimize=True)
            data = buffer.getvalue()
    except PixelArtError:
        raise
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise PixelArtError(f"не удалось прочитать PNG: {exc}") from exc

    if len(data) > MAX_OUTPUT_BYTES:
        raise PixelArtError("пиксельный PNG получился больше 3 МБ")
    return data
