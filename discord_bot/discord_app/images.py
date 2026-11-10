"""Discord image attachments -> OpenAI-compatible vision parts.

Only images from the current message are sent to the model. Everything else
(PDF, archives, video) is ignored: the free-tier deployment has neither the
memory nor the token budget for arbitrary files. Image bytes are never stored
in memory or logged; the transcript keeps a short text marker instead.
"""

from __future__ import annotations

import asyncio
import base64
import logging
from typing import Any

logger = logging.getLogger(__name__)

MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_IMAGES_PER_MESSAGE = 2
DOWNLOAD_TIMEOUT_SECONDS = 20.0

MIME_TYPES = {
    "image/png": "image/png",
    "image/jpeg": "image/jpeg",
    "image/jpg": "image/jpeg",
    "image/webp": "image/webp",
    "image/gif": "image/gif",
}

EXTENSIONS = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}


def image_mime_type(attachment: Any) -> str | None:
    """Return a supported image MIME type, or None for a non-image file."""

    content_type = str(getattr(attachment, "content_type", "") or "").lower()
    if content_type in MIME_TYPES:
        return MIME_TYPES[content_type]

    filename = str(getattr(attachment, "filename", "") or "").lower()
    for extension, mime in EXTENSIONS.items():
        if filename.endswith(extension):
            return mime
    return None


def _limit_note(filename: str, reason: str) -> str:
    return f"[изображение {filename} пропущено: {reason}]"


async def collect_image_parts(message: Any) -> tuple[list[dict[str, Any]], str]:
    """Build vision content parts for the message's images.

    Returns ``(parts, note)``: ``parts`` are OpenAI-style ``image_url`` entries,
    ``note`` is a short text summary to put into the prompt and the memory
    transcript. A broken or oversized attachment never fails the turn.
    """

    attachments = list(getattr(message, "attachments", []) or [])
    if not attachments:
        return [], ""

    parts: list[dict[str, Any]] = []
    notes: list[str] = []
    for attachment in attachments:
        filename = str(getattr(attachment, "filename", "") or "изображение")
        mime = image_mime_type(attachment)
        if mime is None:
            # Non-images are out of scope and deserve no noise in the prompt.
            continue

        if len(parts) >= MAX_IMAGES_PER_MESSAGE:
            notes.append(
                _limit_note(
                    filename,
                    f"больше {MAX_IMAGES_PER_MESSAGE} изображений за раз",
                )
            )
            continue

        size = int(getattr(attachment, "size", 0) or 0)
        if size > MAX_IMAGE_BYTES:
            notes.append(_limit_note(filename, "файл слишком большой"))
            continue

        try:
            data = await asyncio.wait_for(
                attachment.read(), timeout=DOWNLOAD_TIMEOUT_SECONDS
            )
        except Exception:  # noqa: BLE001 - one bad attachment must not break the reply
            logger.warning("Не удалось загрузить изображение %s", filename)
            notes.append(_limit_note(filename, "не удалось загрузить"))
            continue

        if not data:
            notes.append(_limit_note(filename, "пустой файл"))
            continue
        if len(data) > MAX_IMAGE_BYTES:
            notes.append(_limit_note(filename, "файл слишком большой"))
            continue

        encoded = base64.b64encode(data).decode("ascii")
        parts.append(
            {
                "type": "image_url",
                "image_url": {"url": f"data:{mime};base64,{encoded}"},
            }
        )
        notes.append(f"[изображение: {filename}]")

    return parts, "\n".join(notes)


def attach_images(
    messages: list[dict[str, Any]], image_parts: list[dict[str, Any]]
) -> None:
    """Attach vision parts to the last user message, in place."""

    if not messages or not image_parts:
        return
    last = messages[-1]
    content = last.get("content")
    text = content if isinstance(content, str) and content.strip() else ""
    if not text:
        text = "Посмотри на приложенное изображение."
    last["content"] = [{"type": "text", "text": text}, *image_parts]
