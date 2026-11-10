from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from discord_bot.memory.models import (
    ConversationState,
    Fact,
    Job,
    StoredMessage,
    Summary,
)
from discord_bot.memory.worker import MemoryWorker


def stored(
    message_id: int,
    author_id: str,
    author_name: str,
    content: str,
    *,
    is_bot: bool = False,
    created_at: datetime | None = None,
) -> StoredMessage:
    return StoredMessage(
        id=message_id,
        discord_message_id=str(message_id),
        author_id=author_id,
        author_name=author_name,
        is_bot=is_bot,
        role="assistant" if is_bot else "user",
        addressed_to="ambient",
        reply_to_id=None,
        content=content,
        created_at=created_at or datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
        session_id="session-1",
    )


class FakeStore:
    def __init__(self, conversation: ConversationState) -> None:
        self.conversation = conversation
        self.open_session: dict[str, Any] | None = {
            "id": "session-1",
            "started_at": datetime(2026, 1, 1, 11, 0, tzinfo=UTC),
            "message_count": 4,
        }
        self.session_items: list[StoredMessage] = []
        self.after_items: list[StoredMessage] = []
        self.facts: list[Fact] = []
        self.summaries: list[Summary] = []
        self.session_summary_count = 0
        self.latest: Summary | None = None

        self.saved_session_summaries: list[dict[str, Any]] = []
        self.saved_conversation_summaries: list[dict[str, Any]] = []
        self.saved_facts: list[dict[str, Any]] = []
        self.closed: list[tuple[str, datetime | None]] = []
        self.markers: list[dict[str, Any]] = []
        self.enqueued: list[dict[str, Any]] = []
        self.purged_messages: list[datetime] = []
        self.purged_facts: list[datetime] = []
        self.unsummarized_sessions: list[dict[str, Any]] = []
        self.requeue_calls = 0
        self.heartbeat: dict[str, Any] | None = None
        self.heartbeat_writes: list[str] = []

    async def get_conversation(self, conversation_id: str) -> ConversationState | None:
        return self.conversation

    async def get_open_session(self, conversation_id: str) -> dict[str, Any] | None:
        return self.open_session

    async def session_messages(
        self, session_id: str, limit: int
    ) -> list[StoredMessage]:
        return list(self.session_items)

    async def save_session_summary(
        self,
        *,
        conversation_id: str,
        session_id: str,
        content: str,
        covers_from: datetime | None,
        covers_to: datetime | None,
    ) -> bool:
        self.saved_session_summaries.append(
            {
                "conversation_id": conversation_id,
                "session_id": session_id,
                "content": content,
                "covers_from": covers_from,
                "covers_to": covers_to,
            }
        )
        return True

    async def close_session(
        self, session_id: str, ended_at: datetime | None
    ) -> bool:
        self.closed.append((session_id, ended_at))
        self.open_session = None
        return True

    async def list_unsummarized_sessions(
        self, conversation_id: str, limit: int = 5
    ) -> list[dict[str, Any]]:
        return list(self.unsummarized_sessions)

    async def messages_after(
        self, conversation_id: str, after_id: int, limit: int
    ) -> list[StoredMessage]:
        return list(self.after_items)

    async def list_facts(
        self,
        user_id: str,
        *,
        include_private: bool,
        guild_id: str | None,
        limit: int = 500,
    ) -> list[Fact]:
        return list(self.facts)

    async def save_facts(
        self,
        user_id: str,
        *,
        scope: str,
        guild_id: str | None,
        facts: list[Fact],
        source_message_id: int | None,
        source_quote: str | None,
    ) -> int:
        self.saved_facts.append(
            {
                "user_id": user_id,
                "scope": scope,
                "guild_id": guild_id,
                "facts": list(facts),
                "source_message_id": source_message_id,
                "source_quote": source_quote,
            }
        )
        return len(facts)

    async def set_markers(
        self,
        conversation_id: str,
        *,
        summary_message_id: int | None = None,
        extracted_message_id: int | None = None,
        summary_at: datetime | None = None,
    ) -> bool:
        self.markers.append(
            {
                "summary_message_id": summary_message_id,
                "extracted_message_id": extracted_message_id,
                "summary_at": summary_at,
            }
        )
        return True

    async def latest_conversation_summary(
        self, conversation_id: str
    ) -> Summary | None:
        return self.latest

    async def count_session_summaries_since(
        self, conversation_id: str, since: datetime | None
    ) -> int:
        return self.session_summary_count

    async def list_summaries(
        self, conversation_id: str, limit: int
    ) -> list[Summary]:
        return list(self.summaries)

    async def save_conversation_summary(
        self,
        *,
        conversation_id: str,
        content: str,
        covers_from: datetime | None,
        covers_to: datetime | None,
    ) -> bool:
        self.saved_conversation_summaries.append(
            {
                "conversation_id": conversation_id,
                "content": content,
                "covers_from": covers_from,
                "covers_to": covers_to,
            }
        )
        return True

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

    async def requeue_stale_jobs(self) -> bool:
        self.requeue_calls += 1
        return True

    async def purge_old_messages(self, older_than: datetime) -> int:
        self.purged_messages.append(older_than)
        return 1

    async def purge_old_facts(self, older_than: datetime) -> int:
        self.purged_facts.append(older_than)
        return 1

    async def read_heartbeat(self) -> dict[str, Any] | None:
        if self.heartbeat is None:
            return None
        return dict(self.heartbeat)

    async def write_heartbeat(self, instance_id: str) -> bool:
        self.heartbeat_writes.append(instance_id)
        self.heartbeat = {"instance_id": instance_id, "updated_at": datetime.now(UTC)}
        return True


class FakeSettings:
    def __init__(self, data: dict[str, str] | None = None) -> None:
        self.data = {
            "worker_llm_interval_seconds": "0",
            **(data or {}),
        }

    async def get_all(self) -> dict[str, str]:
        return dict(self.data)


class FakeLLM:
    def __init__(self, *, summary: str = "Резюме", facts_json: str = "{}") -> None:
        self.summary = summary
        self.facts_json = facts_json
        self.prompts: list[str] = []

    async def complete_text(
        self,
        spec: Any,
        model: str,
        messages: list[dict[str, str]],
        **kwargs: Any,
    ) -> str:
        prompt = messages[-1]["content"]
        self.prompts.append(prompt)
        if "Верни строго JSON" in prompt:
            return self.facts_json
        return self.summary


def conversation(
    *, last_message_at: datetime | None = None, last_extracted: int = 0
) -> ConversationState:
    return ConversationState(
        id="conv-1",
        scope_type="channel",
        scope_id="222",
        guild_id="333",
        last_message_at=last_message_at,
        last_seen_message_id="20",
        last_summary_message_id=0,
        last_extracted_message_id=last_extracted,
    )


async def test_idle_session_is_summarised_and_closed() -> None:
    last = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    store = FakeStore(conversation(last_message_at=last))
    store.session_items = [
        stored(1, "u1", "Аня", "привет"),
        stored(2, "bot", "Бот", "здравствуй", is_bot=True),
        stored(3, "u2", "Вася", "вопрос"),
    ]
    llm = FakeLLM(summary="Аня поздоровалась, Вася спросил.")
    worker = MemoryWorker(store, FakeSettings(), llm)

    await worker._process_job(
        Job(id=1, kind="session_close", conversation_id="conv-1")
    )

    assert store.saved_session_summaries == [
        {
            "conversation_id": "conv-1",
            "session_id": "session-1",
            "content": "Аня поздоровалась, Вася спросил.",
            "covers_from": datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
            "covers_to": last,
        }
    ]
    assert store.closed == [("session-1", last)]


async def test_active_session_is_not_closed_and_job_is_rearmed() -> None:
    last = datetime.now(UTC) - timedelta(minutes=5)
    store = FakeStore(conversation(last_message_at=last))
    worker = MemoryWorker(store, FakeSettings(), FakeLLM())

    await worker._process_job(
        Job(id=1, kind="session_close", conversation_id="conv-1")
    )

    assert store.closed == []
    assert len(store.enqueued) == 1
    assert store.enqueued[0]["kind"] == "session_close"
    assert store.enqueued[0]["not_before"] == last + timedelta(minutes=30)


async def test_session_opened_after_the_idle_moment_is_not_closed() -> None:
    ended_at = datetime(2026, 1, 1, 10, 0, tzinfo=UTC)
    store = FakeStore(conversation(last_message_at=ended_at))
    store.open_session = {
        "id": "new-session",
        "started_at": datetime(2026, 1, 1, 11, 0, tzinfo=UTC),
        "message_count": 1,
    }
    worker = MemoryWorker(store, FakeSettings(), FakeLLM())

    await worker._close_session(
        conversation(last_message_at=ended_at),
        ended_at,
    )

    assert store.closed == []
    assert store.saved_session_summaries == []


async def test_closed_session_is_summarised_on_the_next_pass() -> None:
    ended_at = datetime(2026, 1, 1, 10, 0, tzinfo=UTC)
    store = FakeStore(conversation(last_message_at=datetime.now(UTC)))
    store.unsummarized_sessions = [
        {
            "id": "session-old",
            "started_at": datetime(2026, 1, 1, 9, 0, tzinfo=UTC),
            "ended_at": ended_at,
        }
    ]
    store.session_items = [
        stored(
            1,
            "u1",
            "Аня",
            "вчера обсуждали деплой",
            created_at=datetime(2026, 1, 1, 9, 30, tzinfo=UTC),
        )
    ]
    llm = FakeLLM(summary="Аня обсуждала деплой.")
    worker = MemoryWorker(store, FakeSettings(), llm)

    await worker._process_job(
        Job(id=1, kind="maintenance", conversation_id="conv-1")
    )

    assert store.saved_session_summaries == [
        {
            "conversation_id": "conv-1",
            "session_id": "session-old",
            "content": "Аня обсуждала деплой.",
            "covers_from": datetime(2026, 1, 1, 9, 30, tzinfo=UTC),
            "covers_to": ended_at,
        }
    ]


async def test_facts_are_extracted_per_author_without_mixing() -> None:
    store = FakeStore(conversation(last_message_at=datetime.now(UTC)))
    store.after_items = [
        stored(1, "u1", "Аня", "я живу в Москве"),
        stored(2, "u2", "Вася", "а я в Питере"),
        stored(3, "u1", "Аня", "и работаю в Яндексе"),
    ]
    store.facts = [
        Fact(id="old", kind="identity", content="уже известно", visibility="private")
    ]
    facts_json = (
        '{"facts": [{"kind": "identity", "content": "живёт в Москве", '
        '"visibility": "private", "confidence": 0.9, "importance": 0.7}]}'
    )
    worker = MemoryWorker(store, FakeSettings(), FakeLLM(facts_json=facts_json))

    await worker._process_job(
        Job(id=1, kind="maintenance", conversation_id="conv-1")
    )

    assert [item["user_id"] for item in store.saved_facts] == ["u1"]
    saved = store.saved_facts[0]
    assert saved["scope"] == "guild"
    assert saved["guild_id"] == "333"
    assert saved["source_message_id"] == 3
    assert [fact.content for fact in saved["facts"]] == ["живёт в Москве"]
    assert store.markers[-1]["extracted_message_id"] == 3


async def test_bad_scores_from_the_model_do_not_fail_extraction() -> None:
    store = FakeStore(conversation(last_message_at=datetime.now(UTC)))
    store.after_items = [
        stored(1, "u1", "Аня", "я живу в Москве"),
        stored(2, "u1", "Аня", "работаю в Яндексе"),
        stored(3, "u1", "Аня", "люблю кофе"),
    ]
    facts_json = (
        '{"facts": [{"kind": "identity", "content": "живёт в Москве", '
        '"confidence": "high", "importance": null}]}'
    )
    worker = MemoryWorker(store, FakeSettings(), FakeLLM(facts_json=facts_json))

    await worker._process_job(
        Job(id=1, kind="maintenance", conversation_id="conv-1")
    )

    saved = store.saved_facts[0]["facts"][0]
    assert saved.confidence == 0.7
    assert saved.importance == 0.5


async def test_rollup_runs_after_three_session_summaries() -> None:
    store = FakeStore(conversation(last_message_at=datetime.now(UTC)))
    store.session_summary_count = 3
    store.summaries = [
        Summary(
            level="session",
            content=f"сессия {index}",
            created_at=datetime(2026, 1, index, tzinfo=UTC),
        )
        for index in (1, 2, 3)
    ]
    llm = FakeLLM(summary="Общее резюме")
    worker = MemoryWorker(store, FakeSettings(), llm)

    await worker._process_job(
        Job(id=1, kind="maintenance", conversation_id="conv-1")
    )

    assert store.saved_conversation_summaries == [
        {
            "conversation_id": "conv-1",
            "content": "Общее резюме",
            "covers_from": datetime(2026, 1, 1, tzinfo=UTC),
            "covers_to": datetime(2026, 1, 3, tzinfo=UTC),
        }
    ]
    assert store.markers[-1]["summary_at"] is not None


async def test_retention_runs_once_per_day() -> None:
    store = FakeStore(conversation())
    worker = MemoryWorker(store, FakeSettings({"memory_retention_days": "30"}), FakeLLM())

    await worker._maybe_retention()
    await worker._maybe_retention()

    assert len(store.purged_messages) == 1
    assert len(store.purged_facts) == 1
    cutoff = store.purged_messages[0]
    assert datetime.now(UTC) - cutoff >= timedelta(days=29)


async def test_stale_jobs_are_requeued_periodically() -> None:
    store = FakeStore(conversation())
    worker = MemoryWorker(store, FakeSettings(), FakeLLM())

    await worker._maybe_requeue_stale()
    await worker._maybe_requeue_stale()

    assert store.requeue_calls == 1


async def test_heartbeat_writes_the_instance_id() -> None:
    store = FakeStore(conversation())
    worker = MemoryWorker(store, FakeSettings(), FakeLLM(), instance_id="abc123")

    await worker._heartbeat(force=True)

    assert store.heartbeat_writes == ["abc123"]
    assert store.heartbeat is not None
    assert store.heartbeat["instance_id"] == "abc123"


async def test_heartbeat_is_throttled_to_once_a_minute() -> None:
    store = FakeStore(conversation())
    worker = MemoryWorker(store, FakeSettings(), FakeLLM(), instance_id="abc123")

    await worker._heartbeat()
    await worker._heartbeat()

    assert len(store.heartbeat_writes) == 1


async def test_fresh_heartbeat_from_another_instance_is_reported(
    caplog: pytest.LogCaptureFixture,
) -> None:
    store = FakeStore(conversation())
    store.heartbeat = {
        "instance_id": "other-instance",
        "updated_at": datetime.now(UTC) - timedelta(seconds=10),
    }
    worker = MemoryWorker(store, FakeSettings(), FakeLLM(), instance_id="me")

    with caplog.at_level(logging.ERROR, logger="discord_bot.memory.worker"):
        await worker._check_other_instance()

    assert any(
        "второй живой инстанс" in record.message for record in caplog.records
    )


async def test_own_fresh_heartbeat_is_silent(
    caplog: pytest.LogCaptureFixture,
) -> None:
    store = FakeStore(conversation())
    store.heartbeat = {
        "instance_id": "me",
        "updated_at": datetime.now(UTC) - timedelta(seconds=10),
    }
    worker = MemoryWorker(store, FakeSettings(), FakeLLM(), instance_id="me")

    with caplog.at_level(logging.ERROR, logger="discord_bot.memory.worker"):
        await worker._check_other_instance()

    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


async def test_stale_heartbeat_from_another_instance_is_silent(
    caplog: pytest.LogCaptureFixture,
) -> None:
    store = FakeStore(conversation())
    store.heartbeat = {
        "instance_id": "other-instance",
        "updated_at": datetime.now(UTC) - timedelta(hours=2),
    }
    worker = MemoryWorker(store, FakeSettings(), FakeLLM(), instance_id="me")

    with caplog.at_level(logging.ERROR, logger="discord_bot.memory.worker"):
        await worker._check_other_instance()

    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
