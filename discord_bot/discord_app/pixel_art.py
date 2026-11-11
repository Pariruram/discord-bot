"""Turn a rendered PNG into crisp pixel art with a fixed block size."""

from __future__ import annotations

import io
import logging

from PIL import Image, UnidentifiedImageError

logger = logging.getLogger(__name__)

DEFAULT_GRID = 32
DEFAULT_COLORS = 16
DEFAULT_BLOCK = 4
MIN_GRID = 16
MAX_GRID = 128
MIN_COLORS = 8
MAX_COLORS = 32
MIN_BLOCK = 2
MAX_BLOCK = 24
MAX_OUTPUT_BYTES = 3 * 1024 * 1024


class PixelArtError(Exception):
    """A user-facing pixel-art conversion failure."""


def pixelate(
    png: bytes,
    *,
    grid: int = DEFAULT_GRID,
    colors: int = DEFAULT_COLORS,
    block: int = DEFAULT_BLOCK,
    nearest: bool = False,
) -> bytes:
    """Downscale, quantize and nearest-upscale a PNG.

    ``grid`` is the number of pixels along the *longest* side; ``block`` is
    the physical size of every output pixel. With the defaults (32 and 4) the
    largest output side is exactly 128 px, and every pixel is 4×4 regardless
    of the source aspect ratio. ``nearest`` preserves a native pixel grid
    instead of averaging it with BOX.
    """

    if not png.startswith(b"\x89PNG\r\n\x1a\n"):
        raise PixelArtError("на входе не PNG")
    grid = max(MIN_GRID, min(MAX_GRID, grid))
    colors = max(MIN_COLORS, min(MAX_COLORS, colors))
    block = max(MIN_BLOCK, min(MAX_BLOCK, block))

    try:
        with Image.open(io.BytesIO(png)) as source:
            rgba = source.convert("RGBA")
            if rgba.width <= 0 or rgba.height <= 0:
                raise PixelArtError("пустое изображение")
            background = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
            rgb = Image.alpha_composite(background, rgba).convert("RGB")

            long_side = max(rgb.width, rgb.height)
            target_w = max(1, round(rgb.width * grid / long_side))
            target_h = max(1, round(rgb.height * grid / long_side))
            longest = max(target_w, target_h)
            if longest > grid:
                scale = grid / longest
                target_w = max(1, round(target_w * scale))
                target_h = max(1, round(target_h * scale))

            small = rgb.resize(
                (target_w, target_h),
                Image.Resampling.NEAREST
                if nearest
                else Image.Resampling.BOX,
            )
            quantized = small.quantize(
                colors=colors,
                method=Image.Quantize.MEDIANCUT,
                dither=Image.Dither.NONE,
            ).convert("RGB")

            output = quantized.resize(
                (target_w * block, target_h * block),
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


def upscale_nearest(png: bytes, factor: int = 4) -> bytes:
    """Upscale a pixel-art PNG without smoothing."""

    factor = max(1, factor)
    try:
        with Image.open(io.BytesIO(png)) as source:
            output = source.convert("RGB").resize(
                (source.width * factor, source.height * factor),
                Image.Resampling.NEAREST,
            )
            buffer = io.BytesIO()
            output.save(buffer, format="PNG", optimize=True)
            data = buffer.getvalue()
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise PixelArtError(f"не удалось увеличить PNG: {exc}") from exc
    if len(data) > MAX_OUTPUT_BYTES:
        raise PixelArtError("увеличенный PNG получился больше 3 МБ")
    return data
