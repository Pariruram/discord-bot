"""Message pipeline: addressing, memory, model call, tools and rendering."""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

import discord
from discord.ext import commands

from discord_bot.citations import format_sources
from discord_bot.discord_app.addressing import classify_message
from discord_bot.llm.client import LLMClient, LLMError
from discord_bot.llm.models import resolve_model_ref
from discord_bot.llm.providers import build_providers
from discord_bot.llm.tool_loop import run_tool_loop
from discord_bot.memory.service import MemoryService
from discord_bot.memory.settings import MemorySettings
from discord_bot.storage.cache import SettingsCache
from discord_bot.tools.base import ToolContext, ToolRegistry, enabled_tool_names
from discord_bot.utils import safe_int, split_message

logger = logging.getLogger(__name__)

MAX_DISCORD_MESSAGE = 2000


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
        except Exception:  # noqa: BLE001 - top-level safety net for one message
            error_id = uuid.uuid4().hex[:8]
            logger.exception("Ошибка обработки сообщения (id=%s)", error_id)
            await self._reply_safe(
                message,
                f"Произошла ошибка, попробуй ещё раз. (код: {error_id})",
            )

    async def _handle(self, message: discord.Message) -> None:
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

        result = await self.memory.ingest(message, incoming)

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

        clean = self._clean_content(message)
        scope_key = f"{channel.id}:{message.author.id}"
        lock = self._lock_for(scope_key)

        async with message.channel.typing():
            async with lock:
                system_prompt, chat_messages = await self.memory.build_context(
                    message,
                    incoming,
                    result,
                    settings,
                    clean,
                )
                messages: list[dict[str, Any]] = [
                    {"role": "system", "content": system_prompt},
                    *chat_messages,
                ]

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
                    "citations": [
                        {"title": citation.title, "url": citation.url}
                        for citation in tool_context.citations
                    ],
                }
                await self.memory.record_reply(message, result, reply, metadata)

        max_len = safe_int(
            settings.get("max_message_length"),
            1900,
            minimum=200,
            maximum=MAX_DISCORD_MESSAGE,
        )
        await self._send(message, sent, max_len)

    def _lock_for(self, key: str) -> asyncio.Lock:
        if len(self._locks) > 1000:
            for old_key, lock in list(self._locks.items()):
                if old_key != key and not lock.locked():
                    self._locks.pop(old_key, None)
        return self._locks.setdefault(key, asyncio.Lock())

    def _clean_content(self, message: discord.Message) -> str:
        content = message.content or ""
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
        except discord.HTTPException:
            logger.exception("Не удалось отправить ответ в канал %s", message.channel.id)

    async def _reply_safe(self, message: discord.Message, text: str) -> None:
        try:
            await message.reply(text)
        except discord.HTTPException:
            logger.exception("Не удалось отправить сообщение об ошибке")
