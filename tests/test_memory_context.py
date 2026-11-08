from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import discord

from discord_bot.memory.context import build_context, format_transcript
from discord_bot.memory.models import Fact, IngestResult, StoredMessage, Summary


def message_at(
    message_id: int,
    author_name: str,
    content: str,
    *,
    is_bot: bool = False,
    role: str = "user",
    reply_to_id: str | None = None,
    created_at: datetime | None = None,
) -> StoredMessage:
    return StoredMessage(
        id=message_id,
        discord_message_id=str(message_id),
        author_id=None if is_bot else f"user-{author_name}",
        author_name=author_name,
        is_bot=is_bot,
        role=role,
        addressed_to="ambient",
        reply_to_id=reply_to_id,
        content=content,
        created_at=created_at or datetime(2026, 1, 1, tzinfo=UTC),
        session_id="session-1",
    )


class FakeStore:
    def __init__(
        self,
        *,
        persona: dict | None = None,
        facts: list[Fact] | None = None,
        summaries: list[Summary] | None = None,
        history: list[StoredMessage] | None = None,
    ) -> None:
        self.persona = persona or {}
        self.facts = facts or []
        self.summaries = summaries or []
        self.history = history or []
        self.fact_queries: list[dict] = []
        self.history_queries: list[dict] = []

    async def get_persona(self, user_id: str) -> dict:
        return self.persona

    async def list_facts(
        self,
        user_id: str,
        *,
        include_private: bool,
        guild_id: str | None,
        limit: int = 500,
    ) -> list[Fact]:
        self.fact_queries.append(
            {
                "user_id": user_id,
                "include_private": include_private,
                "guild_id": guild_id,
            }
        )
        return list(self.facts)

    async def list_summaries(self, conversation_id: str, limit: int) -> list[Summary]:
        return list(self.summaries)

    async def history_before(
        self, conversation_id: str, before_id: int, limit: int
    ) -> list[StoredMessage]:
        self.history_queries.append(
            {
                "conversation_id": conversation_id,
                "before_id": before_id,
                "limit": limit,
            }
        )
        return list(self.history)


def guild_message() -> SimpleNamespace:
    return SimpleNamespace(
        channel=SimpleNamespace(type=discord.ChannelType.text, id=222),
        guild=SimpleNamespace(id=333),
        author=SimpleNamespace(id=111, display_name="Аня", name="anya"),
    )


def dm_message() -> SimpleNamespace:
    return SimpleNamespace(
        channel=SimpleNamespace(type=discord.ChannelType.private, id=444),
        guild=None,
        author=SimpleNamespace(id=111, display_name="Аня", name="anya"),
    )


RESULT = IngestResult(message_id=10, conversation_id="conv-1", session_id="sess-1")


def test_format_transcript_is_oldest_first_and_marks_authors() -> None:
    history = [
        message_at(1, "Вася", "привет"),
        message_at(2, "Бот", "здравствуй", is_bot=True, role="assistant"),
        message_at(3, "Аня", "вопрос", reply_to_id="2"),
    ]

    transcript = format_transcript(history)

    assert transcript == (
        "Вася: привет\n"
        "Бот: здравствуй\n"
        "Аня (в ответ Бот): вопрос"
    )


def test_format_transcript_skips_system_and_truncates_to_budget() -> None:
    history = [
        message_at(1, "Вася", "system note", role="system"),
        message_at(2, "Аня", "x" * 100),
    ]

    transcript = format_transcript(
        history, limit_chars=80, max_message_chars=10
    )

    assert "system note" not in transcript
    assert transcript.startswith("Аня: xxxxxxxxx")
    assert transcript.endswith("…")


async def test_channel_context_contains_transcript_and_public_facts() -> None:
    store = FakeStore(
        persona={
            "persona": {"стиль": "коротко"},
            "prompt_hint": "любит примеры",
        },
        facts=[
            Fact(
                id="f1",
                kind="preference",
                content="любит Python",
                visibility="public",
            )
        ],
        summaries=[Summary(level="session", content="Обсуждали тесты.")],
        history=[
            message_at(1, "Вася", "привет"),
            message_at(2, "Аня", "как дела?"),
        ],
    )

    system, chat = await build_context(
        store,
        {"global_system_prompt": "Ты — бот.", "max_history": "5"},
        guild_message(),
        SimpleNamespace(addressed_to="bot"),
        RESULT,
        "что нового?",
    )

    assert "<channel_transcript" in system
    assert "Вася: привет" in system
    assert "Аня: как дела?" in system
    assert "- любит Python" in system
    assert "- стиль: коротко" in system
    assert "- заметка владельца: любит примеры" in system
    assert "Обсуждали тесты." in system
    assert chat == [{"role": "user", "content": "[Аня] что нового?"}]

    assert store.fact_queries == [
        {"user_id": "111", "include_private": False, "guild_id": "333"}
    ]
    assert store.history_queries == [
        {"conversation_id": "conv-1", "before_id": 10, "limit": 5}
    ]


async def test_dm_context_uses_dialogue_history_and_private_facts() -> None:
    store = FakeStore(
        persona={},
        facts=[
            Fact(id="f1", kind="identity", content="живёт в Москве", visibility="private")
        ],
        history=[
            message_at(1, "Аня", "привет"),
            message_at(2, "Бот", "здравствуй", is_bot=True, role="assistant"),
        ],
    )

    system, chat = await build_context(
        store,
        {},
        dm_message(),
        SimpleNamespace(addressed_to="bot"),
        RESULT,
        "как дела?",
    )

    assert "личном диалоге" in system
    assert "<channel_transcript" not in system
    assert "- живёт в Москве" in system
    assert chat == [
        {"role": "user", "content": "привет"},
        {"role": "assistant", "content": "здравствуй"},
        {"role": "user", "content": "как дела?"},
    ]
    assert store.fact_queries[0]["include_private"] is True
    assert store.fact_queries[0]["guild_id"] is None


async def test_context_without_ingest_result_has_no_history() -> None:
    store = FakeStore()

    system, chat = await build_context(
        store,
        {},
        guild_message(),
        SimpleNamespace(addressed_to="bot"),
        None,
        "привет",
    )

    assert "<memory_summary>" not in system
    assert "<channel_transcript" not in system
    assert chat == [{"role": "user", "content": "[Аня] привет"}]
    assert store.history_queries == []


async def test_disabled_memory_is_not_used_for_context() -> None:
    store = FakeStore(
        facts=[Fact(id="f1", kind="identity", content="секрет", visibility="private")],
        history=[message_at(1, "Аня", "старое сообщение")],
    )

    system, chat = await build_context(
        store,
        {"memory_enabled": "false"},
        dm_message(),
        SimpleNamespace(addressed_to="bot"),
        RESULT,
        "привет",
    )

    assert "секрет" not in system
    assert "старое сообщение" not in system
    assert chat == [{"role": "user", "content": "привет"}]
    assert store.fact_queries == []
    assert store.history_queries == []
