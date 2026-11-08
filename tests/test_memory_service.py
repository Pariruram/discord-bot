from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import discord

from discord_bot.discord_app.addressing import Addressing
from discord_bot.memory.models import IngestResult
from discord_bot.memory.service import MemoryService, conversation_scope


class FakeStore:
    def __init__(self, *, memory_enabled: bool = True) -> None:
        self.memory_enabled = memory_enabled
        self.ingest_calls: list[dict[str, Any]] = []
        self.enqueued: list[dict[str, Any]] = []
        self.result = IngestResult(
            message_id=5, conversation_id="conv-1", session_id="sess-1"
        )

    async def user_memory_enabled(self, user_id: str) -> bool:
        return self.memory_enabled

    async def ingest(self, **kwargs: Any) -> IngestResult:
        self.ingest_calls.append(kwargs)
        return self.result

    async def enqueue_job(
        self,
        kind: str,
        conversation_id: str,
        *,
        not_before: datetime | None = None,
    ) -> bool:
        self.enqueued.append(
            {
                "kind": kind,
                "conversation_id": conversation_id,
                "not_before": not_before,
            }
        )
        return True


class FakeSettings:
    def __init__(self, data: dict[str, str] | None = None) -> None:
        self.data = dict(data or {})

    async def get_all(self) -> dict[str, str]:
        return dict(self.data)


class FakeWorker:
    def __init__(self) -> None:
        self.notifications = 0

    def notify(self) -> None:
        self.notifications += 1


def guild_message(content: str = "привет") -> SimpleNamespace:
    return SimpleNamespace(
        id=42,
        content=content,
        channel=SimpleNamespace(type=discord.ChannelType.text, id=222),
        guild=SimpleNamespace(id=333),
        author=SimpleNamespace(
            id=111,
            bot=False,
            display_name="Аня",
            name="anya",
        ),
    )


def dm_message(content: str = "привет") -> SimpleNamespace:
    return SimpleNamespace(
        id=43,
        content=content,
        channel=SimpleNamespace(
            type=discord.ChannelType.private,
            id=444,
            recipient=SimpleNamespace(id=111),
        ),
        guild=None,
        author=SimpleNamespace(
            id=111,
            bot=False,
            display_name="Аня",
            name="anya",
        ),
    )


def test_scope_is_channel_thread_or_dm() -> None:
    assert conversation_scope(guild_message()) == ("channel", "222", "333")
    assert conversation_scope(dm_message()) == ("dm", "111", None)

    thread = guild_message()
    thread.channel.type = discord.ChannelType.public_thread
    thread.channel.id = 555
    assert conversation_scope(thread) == ("thread", "555", "333")


async def test_ingest_stores_ambient_messages_and_queues_jobs() -> None:
    store = FakeStore()
    worker = FakeWorker()
    service = MemoryService(store, FakeSettings(), llm=None)
    service.attach_worker(worker)

    result = await service.ingest(
        guild_message(),
        Addressing(addressed_to="ambient", reply_to_id="7"),
    )

    assert result is not None
    call = store.ingest_calls[0]
    assert call["scope_type"] == "channel"
    assert call["scope_id"] == "222"
    assert call["guild_id"] == "333"
    assert call["discord_message_id"] == "42"
    assert call["author_id"] == "111"
    assert call["author_name"] == "Аня"
    assert call["is_bot"] is False
    assert call["role"] == "user"
    assert call["addressed_to"] == "ambient"
    assert call["reply_to_id"] == "7"

    kinds = [item["kind"] for item in store.enqueued]
    assert kinds == ["maintenance", "session_close"]
    delay = store.enqueued[1]["not_before"] - datetime.now(UTC)
    assert timedelta(minutes=29) < delay <= timedelta(minutes=30, seconds=5)
    assert worker.notifications == 1


async def test_ingest_skips_user_who_disabled_memory() -> None:
    store = FakeStore(memory_enabled=False)
    service = MemoryService(store, FakeSettings(), llm=None)

    result = await service.ingest(
        dm_message(),
        Addressing(addressed_to="bot"),
    )

    assert result is None
    assert store.ingest_calls == []
    assert store.enqueued == []


async def test_duplicate_discord_delivery_does_not_queue_jobs() -> None:
    store = FakeStore()
    store.result = IngestResult(
        message_id=5,
        conversation_id="conv-1",
        session_id="sess-1",
        duplicate=True,
    )
    service = MemoryService(store, FakeSettings(), llm=None)

    await service.ingest(guild_message(), Addressing(addressed_to="bot"))

    assert len(store.ingest_calls) == 1
    assert store.enqueued == []


async def test_bot_reply_is_stored_as_assistant_message() -> None:
    store = FakeStore()
    worker = FakeWorker()
    service = MemoryService(store, FakeSettings(), llm=None)
    service.attach_worker(worker)
    service._bot = SimpleNamespace(
        user=SimpleNamespace(id=999, display_name="Бот", name="bot")
    )
    result = IngestResult(message_id=5, conversation_id="conv-1", session_id="sess-1")

    await service.record_reply(
        guild_message("вопрос"),
        result,
        "ответ",
        {"model": "gemini-3.8-flash"},
    )

    call = store.ingest_calls[0]
    assert call["discord_message_id"] is None
    assert call["author_id"] == "999"
    assert call["author_name"] == "Бот"
    assert call["is_bot"] is True
    assert call["role"] == "assistant"
    assert call["addressed_to"] == "ambient"
    assert call["reply_to_id"] == "42"
    assert call["content"] == "ответ"
    assert call["metadata"] == {"model": "gemini-3.8-flash"}
    assert worker.notifications == 1


async def test_record_reply_without_ingested_message_is_a_noop() -> None:
    store = FakeStore()
    service = MemoryService(store, FakeSettings(), llm=None)

    await service.record_reply(guild_message(), None, "ответ")

    assert store.ingest_calls == []


async def test_memory_can_be_disabled_from_settings() -> None:
    store = FakeStore()
    service = MemoryService(
        store, FakeSettings({"memory_enabled": "false"}), llm=None
    )

    result = await service.ingest(
        guild_message(), Addressing(addressed_to="bot")
    )

    assert result is None
    assert store.ingest_calls == []
