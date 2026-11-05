"""Utility LLM calls: preference extraction and channel summarisation."""

from __future__ import annotations

import json
import logging
from typing import Any

from bot.llm.client import LLMClient, LLMError
from bot.llm.providers import ProviderSpec

logger = logging.getLogger(__name__)


def _strip_code_fence(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else ""
        if text.endswith("```"):
            text = text[:-3]
    return text.strip()


async def extract_preferences(
    llm: LLMClient,
    spec: ProviderSpec,
    model: str,
    user_message: str,
    current_preferences: dict[str, Any],
) -> dict[str, Any]:
    """Ask the utility model for explicit preference changes.

    Failures are non-fatal: the current preferences are returned unchanged.
    """

    prompt = (
        f"Текущие предпочтения: {json.dumps(current_preferences, ensure_ascii=False)}\n"
        f"Сообщение: {user_message!r}\n"
        "Верни JSON-объект только с явно выраженными изменениями предпочтений "
        "или {} если изменений нет. Не добавляй пояснений."
    )

    try:
        text = await llm.complete_text(
            spec,
            model,
            [{"role": "user", "content": prompt}],
            max_tokens=200,
            temperature=0.1,
        )
        data = json.loads(_strip_code_fence(text))
        if isinstance(data, dict) and data:
            return {**current_preferences, **data}
    except Exception as exc:  # noqa: BLE001 - preference extraction is best-effort
        logger.warning("Не удалось извлечь предпочтения: %s", exc)

    return current_preferences


async def summarize_messages(
    llm: LLMClient,
    spec: ProviderSpec,
    model: str,
    old_summary: str,
    messages: list[dict[str, str]],
) -> str:
    """Compact channel history into a short summary. Returns old summary on failure."""

    lines = [f"{item.get('author', '?')}: {item.get('content', '')}" for item in messages]
    new_messages = "\n".join(lines)
    prompt = (
        f"Резюме чата:\n{old_summary or '(пусто)'}\n"
        f"Новые сообщения:\n{new_messages}\n"
        "Сделай краткий итог: ключевые темы и суть. Без вводных слов."
    )

    try:
        summary = await llm.complete_text(
            spec,
            model,
            [{"role": "user", "content": prompt}],
            max_tokens=400,
            temperature=0.2,
        )
        return summary or old_summary
    except LLMError as exc:
        logger.warning("Не удалось сжать историю канала: %s", exc)
        return old_summary
