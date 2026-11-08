"""Memory facade used by the chat pipeline and Discord event handlers."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import discord

from discord_bot.discord_app.addressing import (
    Addressing,
    classify_message,
    is_dm_channel,
    is_thread_channel,
)
from discord_bot.memory.context import build_context
from discord_bot.memory.models import ConversationState, IngestResult
from discord_bot.memory.repository import MemoryStore
from discord_bot.memory.settings import MemorySettings
from discord_bot.storage.cache import SettingsCache

if TYPE_CHECKING:
    from discord_bot.memory.worker import MemoryWorker

logger = logging.getLogger(__name__)


def conversation_scope(message: Any) -> tuple[str, str, str | None]:
    """Return ``(scope_type, scope_id, guild_id)`` for a Discord message."""

    channel = getattr(message, "channel", None)
    guild = getattr(message, "guild", None)
    guild_id = str(guild.id) if guild is not None else None

    if is_dm_channel(channel):
        recipient = getattr(channel, "recipient", None)
        author = getattr(message, "author", None)
        owner_id = getattr(recipient, "id", None) or getattr(author, "id", None)
        return "dm", str(owner_id), None

    if is_thread_channel(channel):
        return "thread", str(getattr(channel, "id", "")), guild_id

    return "channel", str(getattr(channel, "id", "")), guild_id


class MemoryService:
    def __init__(
        self,
        store: MemoryStore,
        settings: SettingsCache,
        llm: Any,
    ) -> None:
        self._store = store
        self._settings = settings
        self._llm = llm
        self._worker: MemoryWorker | None = None
        self._bot: Any = None
        self._backfill_task: asyncio.Task[None] | None = None

    @property
    def store(self) -> MemoryStore:
        return self._store

    def attach_worker(self, worker: MemoryWorker) -> None:
        self._worker = worker

    async def config(self) -> MemorySettings:
        return MemorySettings.from_mapping(await self._settings.get_all())

    async def ingest(
        self,
        message: Any,
        incoming: Addressing,
        *,
        backfill: bool = False,
    ) -> IngestResult | None:
        try:
            return await self._ingest(message, incoming, backfill=backfill)
        except Exception:  # noqa: BLE001 - memory must never break a reply
            logger.exception("Не удалось сохранить сообщение в память")
            return None

    async def _ingest(
        self,
        message: Any,
        incoming: Addressing,
        *,
        backfill: bool,
    ) -> IngestResult | None:
        cfg = await self.config()
        if not cfg.enabled:
            return None

        author = message.author
        author_id = str(getattr(author, "id", ""))
        if not getattr(author, "bot", False):
            if not await self._store.user_memory_enabled(author_id):
                return None

        scope_type, scope_id, guild_id = conversation_scope(message)
        author_name = (
            getattr(author, "display_name", None)
            or getattr(author, "name", None)
            or str(author)
        )

        result = await self._store.ingest(
            scope_type=scope_type,
            scope_id=scope_id,
            guild_id=guild_id,
            discord_message_id=str(message.id),
            author_id=author_id if not getattr(author, "bot", False) else None,
            author_name=author_name,
            is_bot=bool(getattr(author, "bot", False)),
            role="assistant" if getattr(author, "bot", False) else "user",
            addressed_to=incoming.addressed_to,
            reply_to_id=incoming.reply_to_id,
            content=(getattr(message, "content", "") or "").strip()
            or "[сообщение без текста]",
            session_gap_minutes=cfg.session_gap_minutes,
            metadata={"backfill": True} if backfill else {},
        )
        if result is not None and not result.duplicate:
            await self._schedule_jobs(result.conversation_id, cfg)
            if self._worker is not None:
                self._worker.notify()
        return result

    async def _schedule_jobs(
        self, conversation_id: str, cfg: MemorySettings
    ) -> None:
        """Queue immediate maintenance and a delayed session-close check.

        The delayed job is what closes a session during silence: without it the
        summary could only be written when the next message arrives.
        """

        now = datetime.now(UTC)
        await self._store.enqueue_job("maintenance", conversation_id)
        await self._store.enqueue_job(
            "session_close",
            conversation_id,
            not_before=now + timedelta(minutes=cfg.session_gap_minutes),
        )

    async def record_reply(
        self,
        message: Any,
        result: IngestResult | None,
        reply: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        if result is None:
            return

        try:
            cfg = await self.config()
            if not cfg.enabled:
                return
            scope_type, scope_id, guild_id = conversation_scope(message)
            bot_user = getattr(self._bot, "user", None)
            bot_id = str(getattr(bot_user, "id", "")) or None
            bot_name = (
                getattr(bot_user, "display_name", None)
                or getattr(bot_user, "name", None)
                or "Бот"
            )

            ingested = await self._store.ingest(
                scope_type=scope_type,
                scope_id=scope_id,
                guild_id=guild_id,
                discord_message_id=None,
                author_id=bot_id,
                author_name=bot_name,
                is_bot=True,
                role="assistant",
                addressed_to="ambient",
                reply_to_id=str(getattr(message, "id", "")) or None,
                content=reply,
                session_gap_minutes=cfg.session_gap_minutes,
                metadata=metadata or {},
            )
            if ingested is not None:
                await self._schedule_jobs(ingested.conversation_id, cfg)
                if self._worker is not None:
                    self._worker.notify()
        except Exception:  # noqa: BLE001 - memory must never block the answer
            logger.exception("Не удалось сохранить ответ бота в память")

    async def build_context(
        self,
        message: Any,
        incoming: Addressing,
        result: IngestResult | None,
        settings: dict[str, str],
        clean: str,
    ) -> tuple[str, list[dict[str, Any]]]:
        bot_user = getattr(self._bot, "user", None)
        return await build_context(
            self._store,
            settings,
            message,
            incoming,
            result,
            clean,
            bot_user=bot_user,
        )

    async def start_background(self, bot: Any) -> None:
        self._bot = bot
        if self._worker is not None:
            self._worker.start()
        if self._backfill_task is None or self._backfill_task.done():
            self._backfill_task = asyncio.create_task(
                self._backfill(bot), name="memory-backfill"
            )

    async def _backfill(self, bot: Any) -> None:
        try:
            cfg = await self.config()
            if cfg.catch_up_limit <= 0:
                return

            conversations = await self._store.list_conversations_to_resume(limit=10)
            for conversation in conversations:
                channel = await self._resolve_channel(bot, conversation)
                if channel is None:
                    continue

                if conversation.scope_type != "dm":
                    parent_id = getattr(channel, "parent_id", None)
                    if not cfg.channel_allowed(
                        conversation.scope_id,
                        str(parent_id) if parent_id is not None else None,
                    ):
                        continue

                last_seen = conversation.last_seen_message_id
                if not last_seen or not last_seen.isdigit():
                    continue

                recovered = 0
                try:
                    async for item in channel.history(
                        after=discord.Object(id=int(last_seen)),
                        limit=cfg.catch_up_limit,
                        oldest_first=True,
                    ):
                        if getattr(item.author, "bot", False):
                            continue
                        incoming = classify_message(item, getattr(bot, "user", None))
                        if (
                            conversation.scope_type != "dm"
                            and not cfg.allowed_channel_ids
                            and incoming.addressed_to != "bot"
                        ):
                            continue
                        await self.ingest(item, incoming, backfill=True)
                        recovered += 1
                        if recovered >= cfg.catch_up_limit:
                            break
                except discord.HTTPException:
                    logger.exception(
                        "Catch-up не удался для %s:%s",
                        conversation.scope_type,
                        conversation.scope_id,
                    )
                    continue

                if recovered:
                    logger.info(
                        "Catch-up %s:%s восстановил %s сообщений",
                        conversation.scope_type,
                        conversation.scope_id,
                        recovered,
                    )
        except Exception:  # noqa: BLE001 - startup catch-up is best-effort
            logger.exception("Ошибка catch-up памяти")

    @staticmethod
    async def _resolve_channel(bot: Any, conversation: ConversationState) -> Any:
        if not conversation.scope_id.isdigit():
            return None
        channel_id = int(conversation.scope_id)

        if conversation.scope_type == "dm":
            user = bot.get_user(channel_id)
            if user is None:
                fetch_user = getattr(bot, "fetch_user", None)
                if callable(fetch_user):
                    try:
                        user = await fetch_user(channel_id)
                    except discord.HTTPException:
                        logger.warning(
                            "Не удалось получить пользователя для ЛС %s", channel_id
                        )
                        return None
            if user is None:
                return None

            dm = getattr(user, "dm_channel", None)
            if dm is not None:
                return dm

            create_dm = getattr(user, "create_dm", None)
            if callable(create_dm):
                try:
                    return await create_dm()
                except discord.HTTPException:
                    logger.warning(
                        "Не удалось открыть ЛС с пользователем %s", channel_id
                    )
                    return None
            return None

        channel = bot.get_channel(channel_id)
        if channel is None:
            get_thread = getattr(bot, "get_thread", None)
            if callable(get_thread):
                channel = get_thread(channel_id)
        return channel

    async def forget_message(self, discord_message_id: int) -> None:
        await self._store.forget_message(str(discord_message_id))

    async def update_message(self, message: Any) -> None:
        content = (getattr(message, "content", "") or "").strip()
        if not content:
            return
        await self._store.update_message(str(message.id), content)

    async def close(self) -> None:
        if self._backfill_task is not None:
            self._backfill_task.cancel()
            try:
                await self._backfill_task
            except (asyncio.CancelledError, Exception):
                pass
            self._backfill_task = None

    @staticmethod
    def now() -> datetime:
        return datetime.now(UTC)
