"""Per-channel short-term memory with locking, LRU eviction and debounced writes."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from bot.storage.repository import Repository
from bot.utils import truncate

logger = logging.getLogger(__name__)

Summarizer = Callable[[str, list[dict[str, str]]], Awaitable[str]]


@dataclass
class _ChannelState:
    summary: str = ""
    messages: list[dict[str, str]] = field(default_factory=list)
    loaded: bool = False
    dirty: bool = False
    version: int = 0
    last_used: float = field(default_factory=time.monotonic)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class ChannelMemoryManager:
    def __init__(
        self,
        repo: Repository,
        *,
        recent_limit: int = 15,
        compact_every: int = 20,
        max_channels: int = 200,
        flush_interval: float = 10.0,
        max_message_chars: int = 500,
    ) -> None:
        self._repo = repo
        self._recent_limit = max(1, recent_limit)
        self._compact_every = max(1, compact_every)
        self._max_channels = max(1, max_channels)
        self._flush_interval = flush_interval
        self._max_message_chars = max_message_chars
        self._states: dict[str, _ChannelState] = {}

    async def _get_state(self, channel_id: str) -> _ChannelState:
        state = self._states.get(channel_id)
        if state is not None:
            state.last_used = time.monotonic()
            return state

        if len(self._states) >= self._max_channels:
            await self._evict_one()

        state = _ChannelState()
        self._states[channel_id] = state
        return state

    async def _evict_one(self) -> None:
        candidates = sorted(self._states.items(), key=lambda item: item[1].last_used)
        for channel_id, state in candidates:
            if state.lock.locked():
                continue
            async with state.lock:
                if state.dirty:
                    saved = await self._repo.save_channel_context(
                        channel_id, state.summary, list(state.messages)
                    )
                    if not saved:
                        logger.warning(
                            "Не удалось сохранить контекст канала %s перед вытеснением",
                            channel_id,
                        )
                        return
                    state.dirty = False
                self._states.pop(channel_id, None)
                return

    async def _ensure_loaded(self, channel_id: str, state: _ChannelState) -> None:
        if state.loaded:
            return
        data = await self._repo.load_channel_context(channel_id)
        state.summary = str(data.get("summary") or "")
        raw_messages = data.get("messages")
        messages: list[dict[str, str]] = []
        if isinstance(raw_messages, list):
            for item in raw_messages:
                if isinstance(item, dict) and "author" in item and "content" in item:
                    messages.append(
                        {"author": str(item["author"]), "content": str(item["content"])}
                    )
        state.messages = messages[-self._recent_limit :]
        state.loaded = True

    async def add_message(
        self,
        channel_id: str,
        author: str,
        content: str,
        *,
        summarize: Summarizer | None = None,
    ) -> None:
        state = await self._get_state(channel_id)
        async with state.lock:
            await self._ensure_loaded(channel_id, state)
            state.messages.append(
                {
                    "author": author,
                    "content": truncate(content, self._max_message_chars),
                }
            )
            state.dirty = True
            state.version += 1
            state.last_used = time.monotonic()

            threshold = self._recent_limit + self._compact_every
            if len(state.messages) > threshold and summarize is not None:
                to_compact = state.messages[: -self._recent_limit]
                state.messages = state.messages[-self._recent_limit :]
                try:
                    state.summary = await summarize(state.summary, to_compact)
                except Exception:  # noqa: BLE001 - never lose messages on failure
                    logger.exception("Не удалось сжать контекст канала %s", channel_id)
                    state.messages = to_compact + state.messages

    async def get_context(self, channel_id: str) -> str:
        state = await self._get_state(channel_id)
        async with state.lock:
            await self._ensure_loaded(channel_id, state)
            parts: list[str] = []
            if state.summary:
                parts.append(f"Резюме канала:\n{state.summary}")
            if state.messages:
                lines = [
                    f"{item['author']}: {item['content']}" for item in state.messages
                ]
                parts.append("Последние сообщения:\n" + "\n".join(lines))
            return "\n\n".join(parts)

    async def flush_dirty(self) -> None:
        for channel_id, state in list(self._states.items()):
            if not state.dirty:
                continue
            async with state.lock:
                if not state.dirty:
                    continue
                summary = state.summary
                messages = list(state.messages)
                version = state.version
            saved = await self._repo.save_channel_context(channel_id, summary, messages)
            if not saved:
                logger.warning("Не удалось сохранить контекст канала %s", channel_id)
                continue
            async with state.lock:
                # Do not clear the flag if a message arrived while we were saving.
                if state.version == version:
                    state.dirty = False

    async def run_flusher(self) -> None:
        while True:
            await asyncio.sleep(self._flush_interval)
            await self.flush_dirty()

    async def flush_all(self) -> None:
        await self.flush_dirty()
