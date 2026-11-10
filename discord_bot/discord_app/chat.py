"""Message pipeline: addressing, memory, model call, tools and rendering."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import uuid
from collections.abc import AsyncIterator
from typing import Any

import discord
from discord.ext import commands

from discord_bot.citations import format_sources
from discord_bot.discord_app.addressing import classify_message
from discord_bot.discord_app.images import attach_images, collect_image_parts
from discord_bot.llm.client import LLMClient, LLMError
from discord_bot.llm.models import resolve_model_ref
from discord_bot.llm.providers import build_providers
from discord_bot.llm.tool_loop import run_tool_loop
from discord_bot.memory.service import MemoryService, conversation_scope
from discord_bot.memory.settings import MemorySettings
from discord_bot.storage.cache import SettingsCache
from discord_bot.tools.base import ToolContext, ToolRegistry, enabled_tool_names
from discord_bot.utils import as_bool, safe_int, split_message

logger = logging.getLogger(__name__)

MAX_DISCORD_MESSAGE = 2000

# ``/c`` (or ``/clean``) at the very end asks for a factual, persona-free
# answer while keeping the whole conversational context.
CLEAN_MARKER_RE = re.compile(r"(?:^|\s)/(?:clean|c)\s*$", re.IGNORECASE)


def split_clean_marker(text: str) -> tuple[str, bool]:
    """Strip a trailing ``/c`` marker; return ``(text, clean_mode)``.

    A bare marker (nothing left after stripping) is not a mode switch, and
    neither is a message that merely ends with ``.com/c`` — the slash must be
    preceded by a boundary, which URLs never provide.
    """

    match = CLEAN_MARKER_RE.search(text)
    if match is None:
        return text, False
    stripped = text[: match.start()].rstrip()
    if not stripped:
        return text, False
    return stripped, True


@contextlib.asynccontextmanager
async def _typing(channel: Any) -> AsyncIterator[None]:
    """Show the typing indicator without failing the reply on a Discord 429."""

    factory = getattr(channel, "typing", None)
    if not callable(factory):
        yield
        return

    manager = factory()
    try:
        await manager.__aenter__()
    except discord.HTTPException as exc:
        logger.warning("Индикатор набора пропущен: %s", exc)
        yield
        return

    try:
        yield
    finally:
        with contextlib.suppress(discord.HTTPException):
            await manager.__aexit__(None, None, None)


class ChatService:
    def __init__(
        self,
        bot: commands.Bot,
        settings: SettingsCache,
        memory: MemoryService,
        llm: LLMClient,
        registry: ToolRegistry,
    ) -> None:
        self.bot = bot
        self.settings = settings
        self.memory = memory
        self.llm = llm
        self.registry = registry
        self._locks: dict[str, asyncio.Lock] = {}

    async def handle(self, message: discord.Message) -> None:
        try:
            await self._handle(message)
        except discord.HTTPException as exc:
            # Sending another message while Discord is rate-limiting us only
            # makes the block worse.
            logger.warning(
                "Discord API ошибка для сообщения %s: %s",
                getattr(message, "id", "?"),
                exc,
            )
        except Exception:  # noqa: BLE001 - top-level safety net for one message
            error_id = uuid.uuid4().hex[:8]
            logger.exception("Ошибка обработки сообщения (id=%s)", error_id)
            await self._reply_safe(
                message,
                f"Произошла ошибка, попробуй ещё раз. (код: {error_id})",
            )

    async def _handle(self, message: discord.Message) -> None:
        # Serialise the whole turn per conversation. The lock is taken before
        # the first await, so messages are answered in arrival order and the
        # next turn definitely sees the previous bot reply in the history.
        scope_type, scope_id, _ = conversation_scope(message)
        lock = self._lock_for(f"{scope_type}:{scope_id}")
        async with lock:
            await self._handle_locked(message)

    async def _handle_locked(self, message: discord.Message) -> None:
        settings = await self.settings.get_all()
        cfg = MemorySettings.from_mapping(settings)

        channel = message.channel
        if not isinstance(channel, discord.DMChannel):
            parent_id = getattr(channel, "parent_id", None)
            if not cfg.channel_allowed(
                str(channel.id),
                str(parent_id) if parent_id is not None else None,
            ):
                return

        incoming = classify_message(message, self.bot.user)
        if incoming.addressed_to != "bot" and not cfg.allowed_channel_ids:
            # With no explicit allowlist the bot only remembers conversations
            # that actually involve it, never ambient chatter in every channel.
            return

        user_text, clean_mode = split_clean_marker(message.content or "")
        image_parts, image_note = await collect_image_parts(message)
        stored_text = "\n".join(
            part for part in (user_text, image_note) if part
        )
        result = await self.memory.ingest(
            message, incoming, content=stored_text or None
        )

        if result is not None and result.duplicate:
            # Discord replayed a message after a reconnect; it was already
            # stored and answered, so answering again would duplicate the reply.
            return

        if incoming.addressed_to != "bot":
            return

        providers = build_providers(settings)
        if not providers:
            await self._reply_safe(
                message,
                "Не настроен ни один LLM-провайдер. Добавь API-ключ и выбери модель через `!models`.",
            )
            return

        try:
            model_ref = resolve_model_ref(settings, providers)
        except ValueError:
            await self._reply_safe(message, "Не удалось определить модель. Проверь `!models`.")
            return
        spec = providers[model_ref.provider]

        clean = self._clean_content(message, text=stored_text)

        async with _typing(message.channel):
            stats: dict[str, Any] = {}
            system_prompt, chat_messages = await self.memory.build_context(
                message,
                incoming,
                result,
                settings,
                clean,
                stats=stats,
                clean_mode=clean_mode,
            )
            messages: list[dict[str, Any]] = [
                {"role": "system", "content": system_prompt},
                *chat_messages,
            ]
            attach_images(messages, image_parts)

            tool_context = ToolContext(
                user_id=str(message.author.id),
                channel_id=str(channel.id),
                guild_id=str(message.guild.id) if message.guild else None,
                settings=settings,
            )
            enabled = enabled_tool_names(
                settings.get("enabled_tools"), self.registry
            )
            max_tokens = safe_int(
                settings.get("max_tokens"), 1200, minimum=200, maximum=4000
            )

            try:
                reply = await run_tool_loop(
                    self.llm,
                    spec,
                    model_ref,
                    messages,
                    self.registry,
                    enabled,
                    tool_context,
                    max_tokens=max_tokens,
                )
            except LLMError as exc:
                logger.warning("LLM недоступна: %s", exc)
                await self._reply_safe(message, exc.user_message())
                return

            sources = format_sources(reply, tool_context.citations)
            sent = f"{reply}{sources}" if sources else reply

            metadata = {
                "provider": spec.name,
                "model": model_ref.model,
                "tool_calls": tool_context.tool_trace,
                "citations": [
                    {"title": citation.title, "url": citation.url}
                    for citation in tool_context.citations
                ],
            }
            await self.memory.record_reply(message, result, reply, metadata)
            debug_mode = as_bool(settings.get("debug_mode"), False)

        max_len = safe_int(
            settings.get("max_message_length"),
            1900,
            minimum=200,
            maximum=MAX_DISCORD_MESSAGE,
        )
        await self._send(message, sent, max_len)
        if debug_mode:
            await self._send_debug(message, spec, model_ref, tool_context, stats)

    async def _send_debug(
        self,
        message: discord.Message,
        spec: Any,
        model_ref: Any,
        tool_context: ToolContext,
        stats: dict[str, Any],
    ) -> None:
        """One factual line about this turn, no persona involved."""

        tool_parts: list[str] = []
        for entry in tool_context.tool_trace:
            args = entry.get("args") or {}
            args_text = ", ".join(
                f"{key}={str(value)[:60]}" for key, value in args.items()
            )
            mark = " ⨯" if entry.get("error") else ""
            tool_parts.append(f"{entry.get('tool')}({args_text}){mark}")
        tools_text = " · ".join(tool_parts) if tool_parts else "нет"

        history_chars = int(stats.get("context_chars") or 0)
        footer = (
            f"-# 🛠 {getattr(spec, 'name', '?')}/{getattr(model_ref, 'model', '?')}"
            f" | инструменты: {tools_text}"
            f" | контекст: {stats.get('history_messages', '?')} сообщ.,"
            f" {history_chars} симв.,"
            f" фактов: {stats.get('facts', '?')},"
            f" резюме: {'да' if stats.get('summary') else 'нет'}"
        )
        try:
            await message.channel.send(footer, suppress_embeds=True)
        except discord.HTTPException as exc:
            if getattr(exc, "status", None) == 429:
                logger.warning("Discord 429 при отправке debug-строки: %s", exc)
            else:
                logger.exception("Не удалось отправить debug-строку")

    def _lock_for(self, key: str) -> asyncio.Lock:
        if len(self._locks) > 1000:
            for old_key, lock in list(self._locks.items()):
                if old_key != key and not lock.locked():
                    self._locks.pop(old_key, None)
        return self._locks.setdefault(key, asyncio.Lock())

    def _clean_content(self, message: discord.Message, *, text: str | None = None) -> str:
        content = message.content if text is None else text
        if self.bot.user is not None:
            content = content.replace(f"<@{self.bot.user.id}>", "")
            content = content.replace(f"<@!{self.bot.user.id}>", "")
        return content.strip() or "Привет"

    async def _send(self, message: discord.Message, reply: str, max_len: int) -> None:
        chunks = split_message(reply, max_len=max_len)
        if not chunks:
            chunks = ["Извини, не удалось сформировать ответ."]
        try:
            await message.reply(chunks[0], suppress_embeds=True)
            for chunk in chunks[1:]:
                await message.channel.send(chunk, suppress_embeds=True)
        except discord.HTTPException as exc:
            if getattr(exc, "status", None) == 429:
                logger.warning(
                    "Discord 429 при отправке в канал %s: %s",
                    getattr(message.channel, "id", "?"),
                    exc,
                )
            else:
                logger.exception(
                    "Не удалось отправить ответ в канал %s",
                    getattr(message.channel, "id", "?"),
                )

    async def _reply_safe(self, message: discord.Message, text: str) -> None:
        try:
            await message.reply(text)
        except discord.HTTPException as exc:
            if getattr(exc, "status", None) == 429:
                logger.warning("Discord 429 при отправке ошибки: %s", exc)
            else:
                logger.exception("Не удалось отправить сообщение об ошибке")
