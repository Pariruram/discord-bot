"""Pixel-art helpers: nearest-neighbour upscaling only."""

from __future__ import annotations

import io
import logging

from PIL import Image, UnidentifiedImageError

logger = logging.getLogger(__name__)

MAX_OUTPUT_BYTES = 3 * 1024 * 1024


class PixelArtError(Exception):
    """A user-facing pixel-art conversion failure."""


def upscale_nearest(png: bytes, factor: int = 4) -> bytes:
    """Upscale a native pixel-art PNG without smoothing."""

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
