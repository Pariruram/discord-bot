from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import discord

from discord_bot.discord_app.chat import ChatService


class FakeSettings:
    def __init__(self, data: dict[str, str] | None = None) -> None:
        self.data = dict(data or {})

    async def get_all(self) -> dict[str, str]:
        return dict(self.data)


class FakeMemory:
    def __init__(self) -> None:
        self.ingested: list[Any] = []

    async def ingest(self, message: Any, incoming: Any) -> None:
        self.ingested.append((message, incoming))


def message(channel_id: int = 222) -> SimpleNamespace:
    return SimpleNamespace(
        id=42,
        content="обычное сообщение",
        channel=SimpleNamespace(
            type=discord.ChannelType.text,
            id=channel_id,
            parent_id=None,
        ),
        guild=SimpleNamespace(id=333),
        author=SimpleNamespace(id=111, display_name="Аня", name="anya", bot=False),
    )


def chat_service(settings: dict[str, str], memory: FakeMemory) -> ChatService:
    bot = SimpleNamespace(
        user=SimpleNamespace(id=999, mentioned_in=lambda item: False)
    )
    return ChatService(bot, FakeSettings(settings), memory, llm=None, registry=None)


async def test_channel_outside_allowlist_is_ignored_completely() -> None:
    memory = FakeMemory()
    chat = chat_service({"allowed_channel_ids": '["222"]'}, memory)

    await chat._handle(message(channel_id=999))

    assert memory.ingested == []


async def test_ambient_message_in_allowed_channel_is_remembered() -> None:
    memory = FakeMemory()
    chat = chat_service({"allowed_channel_ids": '["222"]'}, memory)

    await chat._handle(message(channel_id=222))

    assert len(memory.ingested) == 1
    _, incoming = memory.ingested[0]
    assert incoming.addressed_to == "ambient"


async def test_ambient_message_is_ignored_without_any_allowlist() -> None:
    memory = FakeMemory()
    chat = chat_service({}, memory)

    await chat._handle(message(channel_id=999))

    assert memory.ingested == []
