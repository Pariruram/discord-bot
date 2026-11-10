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

# Character budgets are defaults; the live limits come from ``bot_settings``
# (``max_context_chars``), so a larger model context can be used via ``!set``.
MAX_TRANSCRIPT_CHARS = 40000
MAX_MESSAGE_CHARS = 600
MAX_DM_MESSAGE_CHARS = 2000
MAX_DM_CONTEXT_CHARS = 40000

SYSTEM_ROLES = {"system"}


def format_dm_history(
    messages: list[StoredMessage],
    *,
    limit_chars: int = MAX_DM_CONTEXT_CHARS,
    max_message_chars: int = MAX_DM_MESSAGE_CHARS,
) -> list[dict[str, Any]]:
    """Return DM turns newest-first budgeted, then back in chronological order."""

    turns: list[dict[str, Any]] = []
    total = 0
    for item in reversed(messages):
        if item.role not in {"user", "assistant"}:
            continue
        content = truncate(item.content, max_message_chars)
        if total + len(content) + 1 > limit_chars:
            break
        turns.append({"role": item.role, "content": content})
        total += len(content) + 1
    return list(reversed(turns))


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


def format_last_actions(meta: dict[str, Any] | None) -> str:
    """Factual description of what the bot actually did on its last turn."""

    if not meta:
        return ""

    lines: list[str] = []
    model = meta.get("model")
    if model:
        provider = meta.get("provider") or ""
        prefix = f"{provider}/" if provider else ""
        lines.append(f"Модель: {prefix}{model}")

    calls = meta.get("tool_calls")
    if isinstance(calls, list) and calls:
        for call in calls:
            if not isinstance(call, dict):
                continue
            name = str(call.get("tool") or "?")
            args = call.get("args")
            if isinstance(args, dict) and args:
                arg_text = ", ".join(f"{k}={v}" for k, v in args.items())
            else:
                arg_text = ""
            outcome = (
                f"ошибка: {call['error']}"
                if call.get("error")
                else f"ок ({call.get('chars', '?')} симв.)"
            )
            lines.append(f"- {name}({arg_text}) → {outcome}"[:400])
        citations = meta.get("citations")
        if isinstance(citations, list) and citations:
            lines.append(f"- источников в ответе: {len(citations)}")
    else:
        lines.append("Инструменты не использовались.")

    return "\n".join(lines)


async def build_context(
    store: MemoryStore,
    settings: dict[str, str],
    message: Any,
    incoming: Addressing,
    result: IngestResult | None,
    clean: str,
    *,
    bot_user: Any | None = None,
    stats: dict[str, Any] | None = None,
    clean_mode: bool = False,
) -> tuple[str, list[dict[str, Any]]]:
    """Return ``(system_prompt, chat_messages)`` for one reply.

    The recent transcript comes from the database; Discord history is only used
    for catch-up after a restart.
    """

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
    recent: list[StoredMessage] = []
    last_actions = ""
    if cfg.enabled and result is not None:
        persona = await store.get_persona(author_id)
        facts = await store.list_facts(
            author_id,
            include_private=is_dm,
            guild_id=guild_id,
            limit=cfg.max_context_facts,
        )

        if result.conversation_id:
            latest = await store.latest_conversation_summary(
                result.conversation_id
            )
            summaries = await store.list_summaries(
                result.conversation_id, limit=3
            )
            parts: list[str] = []
            seen: set[str] = set()
            if latest is not None and latest.content.strip():
                text = latest.content.strip()
                parts.append(text)
                seen.add(text)
            for item in summaries:
                text = item.content.strip()
                if text and text not in seen:
                    parts.append(text)
                    seen.add(text)
            summary = "\n\n".join(parts)
            recent = await store.history_before(
                result.conversation_id,
                result.message_id,
                cfg.max_history,
            )
            reply_meta = await store.latest_reply_metadata(
                result.conversation_id
            )
            last_actions = format_last_actions(reply_meta)

    transcript = ""
    if not is_dm:
        transcript = format_transcript(
            recent, limit_chars=cfg.max_context_chars
        )

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
        last_actions=last_actions,
        clean=clean_mode,
    )

    if is_dm:
        chat: list[dict[str, Any]] = format_dm_history(
            recent, limit_chars=cfg.max_context_chars
        )
        chat.append({"role": "user", "content": clean})
    else:
        chat = [{"role": "user", "content": f"[{author_name}] {clean}"}]

    if stats is not None:
        dm_chars = sum(len(str(turn.get("content", ""))) for turn in chat)
        stats.update(
            history_messages=len(recent),
            context_chars=dm_chars if is_dm else len(transcript),
            facts=len(facts),
            summary=bool(summary),
        )

    return system_prompt, chat
