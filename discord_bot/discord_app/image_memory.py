"""Short text descriptions of user images for persistent memory."""

from __future__ import annotations

from typing import Any

from discord_bot.llm.client import LLMClient
from discord_bot.llm.providers import ProviderSpec

IMAGE_MEMORY_PROMPT = (
    "Опиши, что изображено, для памяти бота: главные объекты, читаемый "
    "текст, для скриншота — интерфейс, ошибки и цифры. 2–4 предложения, "
    "только факты, без домыслов."
)
MAX_DESCRIPTION_CHARS = 500


async def describe_images(
    llm: LLMClient,
    spec: ProviderSpec,
    model: str,
    image_parts: list[dict[str, Any]],
) -> str:
    """Return a compact factual description, or an empty string."""

    if not image_parts:
        return ""
    response = await llm.complete_text(
        spec,
        model,
        [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": IMAGE_MEMORY_PROMPT},
                    *image_parts,
                ],
            }
        ],
        max_tokens=400,
        temperature=0.2,
        max_attempts=2,
        purpose="image_summary",
    )
    return " ".join((response or "").split())[:MAX_DESCRIPTION_CHARS]
