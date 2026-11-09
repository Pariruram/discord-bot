from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import discord

from discord_bot.discord_app.chat import ChatService, _typing
from discord_bot.memory.models import IngestResult


class FakeSettings:
    def __init__(self, data: dict[str, str] | None = None) -> None:
        self.data = dict(data or {})

    async def get_all(self) -> dict[str, str]:
        return dict(self.data)


class FakeMemory:
    def __init__(self, result: IngestResult | None = None) -> None:
        self.ingested: list[Any] = []
        self.result = result

    async def ingest(self, message: Any, incoming: Any) -> IngestResult | None:
        self.ingested.append((message, incoming))
        return self.result


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


async def test_duplicate_delivery_is_not_answered_again() -> None:
    memory = FakeMemory(
        result=IngestResult(
            message_id=5,
            conversation_id="conv-1",
            session_id="sess-1",
            duplicate=True,
        )
    )
    chat = chat_service({}, memory)
    dm = SimpleNamespace(
        id=43,
        content="привет",
        channel=SimpleNamespace(
            type=discord.ChannelType.private,
            id=444,
            parent_id=None,
        ),
        guild=None,
        author=SimpleNamespace(id=111, display_name="Аня", name="anya", bot=False),
        reply=lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("duplicate must not be answered")
        ),
    )

    await chat._handle(dm)

    assert len(memory.ingested) == 1


class RateLimited(discord.HTTPException):
    status = 429
    code = 0
    text = "Too Many Requests"

    def __init__(self) -> None:
        pass


class RateLimitedTyping:
    async def __aenter__(self) -> None:
        raise RateLimited()

    async def __aexit__(self, *args: Any) -> bool:
        return False


class TypingChannel:
    def typing(self) -> RateLimitedTyping:
        return RateLimitedTyping()


async def test_typing_rate_limit_does_not_abort_the_reply() -> None:
    entered = False

    async with _typing(TypingChannel()):
        entered = True

    assert entered is True


async def test_typing_without_a_channel_method_is_optional() -> None:
    async with _typing(SimpleNamespace()):
        pass
