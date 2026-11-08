"""Context assembly: scoped transcript, facts and summaries -> prompt."""

from __future__ import annotations

import logging
from typing import Any

from discord_bot.discord_app.addressing import (
    Addressing,
    is_dm_channel,
)
from discord_bot.memory.models import IngestResult, StoredMessage
from discord_bot.memory.repository import MemoryStore
from discord_bot.memory.settings import MemorySettings
from discord_bot.prompts import build_system_prompt
from discord_bot.utils import truncate

logger = logging.getLogger(__name__)

MAX_TRANSCRIPT_CHARS = 9000
MAX_MESSAGE_CHARS = 600
MAX_DM_MESSAGE_CHARS = 2000

SYSTEM_ROLES = {"system"}


def format_transcript(
    messages: list[StoredMessage],
    *,
    limit_chars: int = MAX_TRANSCRIPT_CHARS,
    max_message_chars: int = MAX_MESSAGE_CHARS,
) -> str:
    """Render a multi-user transcript with explicit authorship.

    Only the newest messages that fit the character budget are kept, then the
    block is returned oldest-first so the conversation still reads naturally.
    """

    names: dict[str, str] = {}
    for item in messages:
        if item.discord_message_id:
            names[item.discord_message_id] = (
                "Бот" if item.is_bot else item.author_name
            )

    lines: list[str] = []
    total = 0
    for item in reversed(messages):
        if item.role in SYSTEM_ROLES:
            continue
        name = "Бот" if item.is_bot else item.author_name
        marker = ""
        if item.reply_to_id:
            target = names.get(item.reply_to_id)
            marker = f" (в ответ {target})" if target else " (в ответ)"
        content = truncate(item.content, max_message_chars)
        line = f"{name}{marker}: {content}"
        if total + len(line) + 1 > limit_chars:
            break
        lines.append(line)
        total += len(line) + 1

    return "\n".join(reversed(lines))


def _persona_lines(row: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    persona = row.get("persona")
    if isinstance(persona, dict):
        for key, value in persona.items():
            text = str(value).strip()
            if text:
                lines.append(f"- {key}: {text}")
    hint = str(row.get("prompt_hint") or "").strip()
    if hint:
        lines.append(f"- заметка владельца: {hint}")
    return lines


async def build_context(
    store: MemoryStore,
    settings: dict[str, str],
    message: Any,
    incoming: Addressing,
    result: IngestResult | None,
    clean: str,
    *,
    bot_user: Any | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    """Return ``(system_prompt, chat_messages)`` for one reply."""

    cfg = MemorySettings.from_mapping(settings)
    is_dm = is_dm_channel(getattr(message, "channel", None))
    author = message.author
    author_id = str(getattr(author, "id", ""))
    author_name = (
        getattr(author, "display_name", None)
        or getattr(author, "name", None)
        or str(author)
    )
    guild_id = str(message.guild.id) if getattr(message, "guild", None) else None

    persona: dict[str, Any] = {}
    facts: list[Any] = []
    summary = ""
    history: list[StoredMessage] = []
    if cfg.enabled:
        persona = await store.get_persona(author_id)
        facts = await store.list_facts(
            author_id,
            include_private=is_dm,
            guild_id=guild_id,
        )

        if result is not None and result.conversation_id:
            summaries = await store.list_summaries(
                result.conversation_id, limit=4
            )
            summary = "\n\n".join(
                item.content.strip()
                for item in summaries
                if item.content.strip()
            )
            history = await store.history_before(
                result.conversation_id,
                result.message_id,
                cfg.max_history,
            )

    transcript = ""
    if not is_dm:
        transcript = format_transcript(history)

    system_prompt = build_system_prompt(
        settings.get("global_system_prompt", ""),
        author_name=author_name,
        author_id=author_id,
        persona=_persona_lines(persona),
        facts=[fact.content for fact in facts],
        summary=summary,
        channel_context=transcript,
        is_dm=is_dm,
        bot_name=str(bot_user) if bot_user is not None else "",
    )

    if is_dm:
        chat: list[dict[str, Any]] = [
            {"role": item.role, "content": truncate(item.content, MAX_DM_MESSAGE_CHARS)}
            for item in history
            if item.role in {"user", "assistant"}
        ]
        chat.append({"role": "user", "content": clean})
    else:
        chat = [{"role": "user", "content": f"[{author_name}] {clean}"}]

    return system_prompt, chat
