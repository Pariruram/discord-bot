"""Message pipeline: memory, history, model call, tools and response rendering."""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

import discord
from discord.ext import commands

from bot.citations import format_sources
from bot.llm.client import LLMClient, LLMError
from bot.llm.models import resolve_model_ref
from bot.llm.providers import build_providers
from bot.llm.tool_loop import run_tool_loop
from bot.llm.utility import extract_preferences, summarize_messages
from bot.prompts import build_system_prompt
from bot.storage.cache import SettingsCache
from bot.storage.channel_memory import ChannelMemoryManager
from bot.storage.repository import Repository
from bot.tools.base import ToolContext, ToolRegistry, enabled_tool_names
from bot.utils import as_bool, safe_int, split_message

logger = logging.getLogger(__name__)

MAX_DISCORD_MESSAGE = 2000


class ChatService:
    def __init__(
        self,
        bot: commands.Bot,
        repo: Repository,
        settings: SettingsCache,
        memory: ChannelMemoryManager,
        llm: LLMClient,
        registry: ToolRegistry,
    ) -> None:
        self.bot = bot
        self.repo = repo
        self.settings = settings
        self.memory = memory
        self.llm = llm
        self.registry = registry
        self._user_locks: dict[str, asyncio.Lock] = {}

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
        providers = build_providers(settings)
        model_ref = resolve_model_ref(settings, providers)
        spec = providers[model_ref.provider]

        is_dm = isinstance(message.channel, discord.DMChannel)
        is_mentioned = bool(self.bot.user and self.bot.user.mentioned_in(message))
        channel_id = str(message.channel.id)

        if not is_dm and (message.content or "").strip():
            async def summarize(old_summary: str, messages: list[dict[str, str]]) -> str:
                return await summarize_messages(
                    self.llm,
                    spec,
                    model_ref.utility_model,
                    old_summary,
                    messages,
                )

            await self.memory.add_message(
                channel_id,
                message.author.display_name,
                message.content.strip(),
                summarize=summarize,
            )

        if not (is_dm or is_mentioned):
            return

        async with message.channel.typing():
            user_id = str(message.author.id)
            lock = self._lock_for(user_id)
            async with lock:
                clean = self._clean_content(message)

                profile = await self.repo.get_user_profile(user_id)
                preferences = profile.get("preferences") or {}
                if not isinstance(preferences, dict):
                    preferences = {}

                if as_bool(settings.get("auto_preferences")):
                    updated = await extract_preferences(
                        self.llm,
                        spec,
                        model_ref.utility_model,
                        clean,
                        preferences,
                    )
                    if updated != preferences:
                        saved = await self.repo.update_preferences(user_id, updated)
                        if not saved:
                            logger.warning(
                                "Предпочтения пользователя %s не сохранены", user_id
                            )
                        preferences = updated

                max_history = safe_int(settings.get("max_history"), 12, minimum=1)

                # Fetch history before writing the current message so it is not
                # duplicated when the database write eventually succeeds.
                history = await self.repo.fetch_recent_history(user_id, max_history)
                await self.repo.save_message(user_id, "user", clean)

                channel_context = (
                    await self.memory.get_context(channel_id) if not is_dm else ""
                )
                system_prompt = build_system_prompt(
                    settings.get("global_system_prompt", ""),
                    preferences,
                    channel_context,
                )

                messages: list[dict[str, Any]] = (
                    [{"role": "system", "content": system_prompt}]
                    + history
                    + [{"role": "user", "content": clean}]
                )

                tool_context = ToolContext(
                    user_id=user_id,
                    channel_id=channel_id,
                    guild_id=str(message.guild.id) if message.guild else None,
                    settings=settings,
                )
                enabled = enabled_tool_names(
                    settings.get("enabled_tools"), self.registry
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
                    )
                except LLMError as exc:
                    logger.warning("LLM недоступна: %s", exc)
                    await self._reply_safe(
                        message,
                        "Не удалось получить ответ от модели. Попробуй позже.",
                    )
                    return

                sources = format_sources(reply, tool_context.citations)
                if sources:
                    reply = f"{reply}{sources}"

                await self.repo.save_message(user_id, "assistant", reply)

        max_len = safe_int(
            settings.get("max_message_length"),
            1900,
            minimum=200,
            maximum=MAX_DISCORD_MESSAGE,
        )
        await self._send(message, reply, max_len)

    def _lock_for(self, user_id: str) -> asyncio.Lock:
        if len(self._user_locks) > 1000:
            for key, lock in list(self._user_locks.items()):
                if key != user_id and not lock.locked():
                    self._user_locks.pop(key, None)
        return self._user_locks.setdefault(user_id, asyncio.Lock())

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
