"""LLM-backed memory extraction: durable facts and session summaries."""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from discord_bot.llm.client import LLMClient
from discord_bot.llm.providers import ProviderSpec
from discord_bot.memory.models import Fact, StoredMessage, Summary
from discord_bot.utils import truncate

logger = logging.getLogger(__name__)

_SPACE_RE = re.compile(r"\s+")
MAX_FACT_CHARS = 240
MAX_SUMMARY_INPUT = 12000


def _strip_code_fence(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else ""
        if text.endswith("```"):
            text = text[:-3]
    return text.strip()


def _load_json_object(text: str) -> dict[str, Any] | None:
    cleaned = _strip_code_fence(text)
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        data = json.loads(cleaned[start : end + 1])
    except (TypeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _normalise(text: str) -> str:
    return _SPACE_RE.sub(" ", text.strip().lower())


def _score(value: Any, default: float) -> float:
    """Coerce an LLM-provided score without letting bad JSON kill the job."""

    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return default


def format_messages(messages: list[StoredMessage], limit: int = MAX_SUMMARY_INPUT) -> str:
    lines: list[str] = []
    total = 0
    for item in messages:
        if item.role == "system" or not item.content.strip():
            continue
        name = "Бот" if item.is_bot else item.author_name
        line = f"{name}: {truncate(item.content, 800)}"
        if total + len(line) + 1 > limit:
            break
        lines.append(line)
        total += len(line) + 1
    return "\n".join(lines)


async def extract_facts(
    llm: LLMClient,
    spec: ProviderSpec,
    model: str,
    author_name: str,
    messages: list[StoredMessage],
    existing: list[str],
) -> list[Fact]:
    """Extract durable facts about one author. LLM failures propagate."""

    transcript = format_messages(messages)
    if not transcript.strip():
        return []

    existing_text = "\n".join(f"- {item}" for item in existing[:100]) or "(пусто)"
    prompt = (
        f"Ты ведёшь долговременную память о пользователе {author_name}.\n"
        "Ниже его сообщения из чата. Извлеки только устойчивые факты о нём:\n"
        "имя, город, работа, интересы, привычки, важные события, отношения.\n"
        "Не добавляй временные детали, догадки и факты о других людях.\n"
        "Не повторяй то, что уже известно.\n\n"
        f"Уже известно:\n{existing_text}\n\n"
        f"Сообщения:\n{transcript}\n\n"
        "Верни строго JSON без пояснений:\n"
        '{"facts": [{"kind": "identity|preference|event|opinion|relation", '
        '"content": "краткий факт от третьего лица", '
        '"visibility": "private|public", '
        '"confidence": 0.0, "importance": 0.0}]}\n'
        "visibility=public только для безобидных фактов, которые человек "
        "сам рассказал в публичном канале. Всё личное — private."
    )

    text = await llm.complete_text(
        spec,
        model,
        [{"role": "user", "content": prompt}],
        max_tokens=1200,
        temperature=0.1,
        max_attempts=1,
        purpose="worker_facts",
    )
    data = _load_json_object(text)
    if data is None:
        logger.warning(
            "Не удалось разобрать JSON фактов для %s (model=%s, chars=%s)",
            author_name,
            model,
            len(text),
        )
        return []

    raw_facts = data.get("facts")
    if not isinstance(raw_facts, list):
        return []

    allowed_kinds = {"identity", "preference", "event", "opinion", "relation"}
    seen: set[str] = set()
    facts: list[Fact] = []
    for item in raw_facts:
        if not isinstance(item, dict):
            continue
        content = str(item.get("content") or "").strip()
        if not content or len(content) > MAX_FACT_CHARS:
            continue
        key = _normalise(content)
        if key in seen:
            continue
        seen.add(key)

        kind = str(item.get("kind") or "identity").strip().lower()
        if kind not in allowed_kinds:
            kind = "identity"
        visibility = str(item.get("visibility") or "private").strip().lower()
        if visibility not in {"private", "public"}:
            visibility = "private"

        facts.append(
            Fact(
                id=None,
                kind=kind,
                content=content,
                visibility=visibility,
                confidence=_score(item.get("confidence"), 0.7),
                importance=_score(item.get("importance"), 0.5),
            )
        )
    return facts[:20]


async def summarize_session(
    llm: LLMClient,
    spec: ProviderSpec,
    model: str,
    messages: list[StoredMessage],
    previous: str = "",
) -> str:
    """Summarise a closed session, preserving who said what."""

    transcript = format_messages(messages)
    if not transcript.strip():
        return previous

    prompt = (
        "Сделай краткое резюме фрагмента переписки на русском языке.\n"
        "Обязательно сохраняй авторство: кто что сказал, спросил или решил.\n"
        "Не теряй имена участников и не приписывай реплики не тем людям.\n"
        "Объём: 3–6 предложений. Без вводных слов.\n\n"
        f"Предыдущее резюме:\n{previous or '(пусто)'}\n\n"
        f"Переписка:\n{transcript}"
    )
    summary = await llm.complete_text(
        spec,
        model,
        [{"role": "user", "content": prompt}],
        max_tokens=500,
        temperature=0.2,
        max_attempts=1,
        purpose="worker_session_summary",
    )
    return summary.strip() or previous


async def summarize_conversation(
    llm: LLMClient,
    spec: ProviderSpec,
    model: str,
    previous: str,
    sessions: list[Summary],
) -> str:
    """Roll several session summaries into one conversation summary."""

    chunks = [item.content.strip() for item in sessions if item.content.strip()]
    if not chunks:
        return previous

    prompt = (
        "Сведи резюме нескольких сессий одного разговора в одно связное резюме "
        "на русском языке. Сохраняй имена и авторство: кто что говорил.\n"
        "Объём: до 8 предложений. Без вводных слов.\n\n"
        f"Предыдущее общее резюме:\n{previous or '(пусто)'}\n\n"
        "Сессии:\n" + "\n\n".join(chunks)
    )
    summary = await llm.complete_text(
        spec,
        model,
        [{"role": "user", "content": prompt}],
        max_tokens=600,
        temperature=0.2,
        max_attempts=1,
        purpose="worker_conversation_summary",
    )
    return summary.strip() or previous
