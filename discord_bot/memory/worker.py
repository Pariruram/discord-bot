"""In-process memory worker.

Render's free plan has no background workers, so this runs as one asyncio task
inside the web process and keeps its queue in Postgres. Jobs survive restarts
and spin-downs; on startup stale ``running`` rows are returned to ``pending``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import UTC, datetime, timedelta

from discord_bot.llm.client import LLMClient
from discord_bot.llm.models import ModelRef, resolve_model_ref
from discord_bot.llm.providers import ProviderSpec, build_providers
from discord_bot.memory.extract import (
    extract_facts,
    summarize_conversation,
    summarize_session,
)
from discord_bot.memory.models import ConversationState, Job, StoredMessage
from discord_bot.memory.repository import MemoryStore
from discord_bot.memory.settings import MemorySettings
from discord_bot.storage.cache import SettingsCache

logger = logging.getLogger(__name__)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


class MemoryWorker:
    def __init__(
        self,
        store: MemoryStore,
        settings: SettingsCache,
        llm: LLMClient,
    ) -> None:
        self._store = store
        self._settings = settings
        self._llm = llm
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._wake = asyncio.Event()
        self._last_retention: datetime | None = None
        self._last_requeue: datetime | None = None

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop = asyncio.Event()
        self._wake = asyncio.Event()
        self._task = asyncio.create_task(self._run(), name="memory-worker")
        logger.info("Memory worker запущен")

    def notify(self) -> None:
        self._wake.set()

    async def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._wake.set()
        task = self._task
        if task is None:
            return
        try:
            await asyncio.wait_for(task, timeout=timeout)
        except (TimeoutError, asyncio.CancelledError):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        finally:
            self._task = None
        logger.info("Memory worker остановлен")

    async def _config(self) -> MemorySettings:
        return MemorySettings.from_mapping(await self._settings.get_all())

    async def _model(self) -> tuple[ProviderSpec, ModelRef] | None:
        settings = await self._settings.get_all()
        providers = build_providers(settings)
        try:
            ref = resolve_model_ref(settings, providers)
        except ValueError:
            return None
        spec = providers.get(ref.provider)
        if spec is None:
            return None
        return spec, ref

    async def _run(self) -> None:
        await self._maybe_requeue_stale()
        while not self._stop.is_set():
            processed = 0
            try:
                processed = await self._process_batch()
                await self._maybe_retention()
                await self._maybe_requeue_stale()
            except Exception:  # noqa: BLE001 - the worker must never kill the bot
                logger.exception("Ошибка memory worker")

            if processed:
                continue

            cfg = await self._config()
            try:
                await asyncio.wait_for(
                    self._wake.wait(),
                    timeout=cfg.worker_poll_seconds,
                )
            except TimeoutError:
                pass
            self._wake.clear()

    async def _process_batch(self) -> int:
        jobs = await self._store.claim_jobs(5)
        for job in jobs:
            try:
                await self._process_job(job)
            except Exception as exc:  # noqa: BLE001 - one job must not stop the rest
                logger.exception("Задача памяти %s не выполнена", job.id)
                await self._store.fail_job(job.id, str(exc))
            else:
                await self._store.finish_job(job.id)
        return len(jobs)

    async def _process_job(self, job: Job) -> None:
        if not job.conversation_id:
            return

        conversation = await self._store.get_conversation(job.conversation_id)
        if conversation is None:
            return

        cfg = await self._config()
        now = datetime.now(UTC)
        last_message = _aware(conversation.last_message_at)
        gap = timedelta(minutes=cfg.session_gap_minutes)
        idle = last_message is not None and now - last_message >= gap

        if job.kind == "session_close":
            await self._summarize_closed_sessions(conversation)
            if idle:
                await self._close_session(conversation, last_message)
                await self._maybe_rollup(conversation)
            elif last_message is not None:
                # The gap or the conversation activity changed after this job
                # was queued. Re-arm it for the actual end of the session.
                await self._store.enqueue_job(
                    "session_close",
                    conversation.id,
                    not_before=last_message + gap,
                )
            return

        await self._summarize_closed_sessions(conversation)
        if idle:
            await self._close_session(conversation, last_message)

        await self._maybe_extract_facts(conversation, cfg)
        await self._maybe_rollup(conversation)

    async def _summarize_closed_sessions(
        self, conversation: ConversationState
    ) -> None:
        """Summarise sessions that ingest closed because of a long gap.

        Without this sweep the session would be rotated by SQL while the bot was
        asleep and its summary would never be written.
        """

        sessions = await self._store.list_unsummarized_sessions(
            conversation.id, limit=5
        )
        for session in sessions:
            session_id = str(session.get("id") or "")
            if not session_id:
                continue
            await self._summarize_session(
                conversation.id,
                session_id,
                _aware(session.get("ended_at")),
            )

    async def _summarize_session(
        self,
        conversation_id: str,
        session_id: str,
        ended_at: datetime | None,
    ) -> None:
        messages = await self._store.session_messages(session_id, limit=200)
        dialog = [item for item in messages if item.content.strip()]
        if not dialog:
            return

        model = await self._model()
        if model is None:
            return
        spec, ref = model

        summary = await summarize_session(
            self._llm,
            spec,
            ref.utility_model,
            dialog,
        )
        if not summary.strip():
            return

        created = [_aware(item.created_at) for item in dialog if item.created_at]
        covers_from = min((item for item in created if item), default=None)
        await self._store.save_session_summary(
            conversation_id=conversation_id,
            session_id=session_id,
            content=summary.strip(),
            covers_from=covers_from,
            covers_to=ended_at,
        )

    async def _close_session(
        self,
        conversation: ConversationState,
        ended_at: datetime,
    ) -> None:
        session = await self._store.get_open_session(conversation.id)
        if session is None:
            return

        session_id = str(session.get("id"))
        started_at = _aware(session.get("started_at"))
        if started_at is not None and started_at > ended_at:
            # A new message already opened the next session while this job was
            # deciding to close the previous one. Never close the new session.
            return

        if not session.get("summary"):
            await self._summarize_session(conversation.id, session_id, ended_at)

        await self._store.close_session(session_id, ended_at)

    async def _maybe_extract_facts(
        self,
        conversation: ConversationState,
        cfg: MemorySettings,
    ) -> None:
        messages = await self._store.messages_after(
            conversation.id,
            conversation.last_extracted_message_id,
            limit=100,
        )
        if len(messages) < 3:
            return

        groups: dict[str, list[StoredMessage]] = {}
        for item in messages:
            if item.is_bot or not item.author_id or not item.content.strip():
                continue
            groups.setdefault(item.author_id, []).append(item)

        model = await self._model()
        if model is None:
            return
        spec, ref = model

        for author_id, group in groups.items():
            if len(group) < 2:
                continue
            existing = await self._store.list_facts(
                author_id,
                include_private=True,
                guild_id=conversation.guild_id,
            )
            remaining = cfg.max_facts_per_user - len(existing)
            if remaining <= 0:
                continue

            facts = await extract_facts(
                self._llm,
                spec,
                ref.utility_model,
                group[-1].author_name,
                group,
                [fact.content for fact in existing],
            )
            if not facts:
                continue

            scope = "guild" if conversation.guild_id else "global"
            source = group[-1]
            await self._store.save_facts(
                author_id,
                scope=scope,
                guild_id=conversation.guild_id,
                facts=facts[:remaining],
                source_message_id=source.id,
                source_quote=source.content,
            )

        await self._store.set_markers(
            conversation.id,
            extracted_message_id=messages[-1].id,
        )

    async def _maybe_rollup(self, conversation: ConversationState) -> None:
        latest = await self._store.latest_conversation_summary(conversation.id)
        since = _aware(latest.created_at) if latest else None

        count = await self._store.count_session_summaries_since(
            conversation.id, since
        )
        if count < 3:
            return

        summaries = await self._store.list_summaries(conversation.id, limit=20)
        sessions = [
            item
            for item in summaries
            if item.level == "session"
            and item.content.strip()
            and (
                since is None
                or (_aware(item.created_at) is not None and _aware(item.created_at) > since)
            )
        ]
        if len(sessions) < 3:
            return

        model = await self._model()
        if model is None:
            return
        spec, ref = model

        content = await summarize_conversation(
            self._llm,
            spec,
            ref.utility_model,
            latest.content if latest else "",
            sessions,
        )
        if not content.strip():
            return

        created = [_aware(item.created_at) for item in sessions if item.created_at]
        await self._store.save_conversation_summary(
            conversation_id=conversation.id,
            content=content.strip(),
            covers_from=min((item for item in created if item), default=None),
            covers_to=max((item for item in created if item), default=None),
        )
        await self._store.set_markers(
            conversation.id,
            summary_at=datetime.now(UTC),
        )

    async def _maybe_requeue_stale(self) -> None:
        """Return jobs abandoned by an earlier process to the queue."""

        now = datetime.now(UTC)
        if (
            self._last_requeue is not None
            and now - self._last_requeue < timedelta(minutes=5)
        ):
            return

        await self._store.requeue_stale_jobs()
        self._last_requeue = now

    async def _maybe_retention(self) -> None:
        now = datetime.now(UTC)
        if self._last_retention is not None and now - self._last_retention < timedelta(hours=24):
            return

        cfg = await self._config()
        cutoff = now - timedelta(days=cfg.retention_days)
        await self._store.purge_old_messages(cutoff)
        await self._store.purge_old_facts(cutoff)
        self._last_retention = now
        logger.info("Retention применён: всё старше %s", cutoff.date())
