from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import discord

from discord_bot.discord_app.chat import (
    ChatService,
    _typing,
    split_clean_marker,
    split_end_markers,
)
from discord_bot.memory.models import IngestResult


def plain_markers(text: str) -> tuple:
    *values, palette = split_end_markers(text)
    assert palette == ""
    return tuple(values)



class FakeSettings:
    def __init__(self, data: dict[str, str] | None = None) -> None:
        self.data = dict(data or {})

    async def get_all(self) -> dict[str, str]:
        return dict(self.data)


class FakeMemory:
    def __init__(self, result: IngestResult | None = None) -> None:
        self.ingested: list[Any] = []
        self.result = result

    async def ingest(
        self, message: Any, incoming: Any, *, content: str | None = None
    ) -> IngestResult | None:
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


def test_clean_marker_is_stripped_and_detected() -> None:
    assert split_clean_marker("что случилось с ИИ /c") == ("что случилось с ИИ", True)
    assert split_clean_marker("что случилось /CLEAN") == ("что случилось", True)


def test_clean_marker_requires_boundaries_and_content() -> None:
    # Обычный текст и URL не активируют режим.
    text, clean = split_clean_marker("https://example.com/c")
    assert clean is False
    text, clean = split_clean_marker("просто сообщение")
    assert clean is False

    # «Голый» маркер — не режим, остаёмся в обычном ответе.
    text, clean = split_clean_marker("/c")
    assert (text, clean) == ("/c", False)


def test_svg_marker_is_stripped_and_detected() -> None:
    assert plain_markers("нарисуй краба /svg") == (
        "нарисуй краба",
        False,
        True,
        False,
        False,
        False,
        False,
    )
    assert plain_markers("нарисуй краба /SVG") == (
        "нарисуй краба",
        False,
        True,
        False,
        False,
        False,
        False,
    )


def test_both_end_markers_work_in_any_order() -> None:
    expected = ("нарисуй схему", True, True, False, False, False, False)

    assert plain_markers("нарисуй схему /c /svg") == expected
    assert plain_markers("нарисуй схему /svg /c") == expected


def test_svg_marker_requires_boundaries_and_content() -> None:
    assert plain_markers("https://example.com/svg") == (
        "https://example.com/svg",
        False,
        False,
        False,
        False,
        False,
        False,
    )
    assert plain_markers("/svg") == (
        "/svg",
        False,
        False,
        False,
        False,
        False,
        False,
    )
    assert plain_markers("нарисуй схему /svg.") == (
        "нарисуй схему /svg.",
        False,
        False,
        False,
        False,
        False,
        False,
    )


def test_pixel_marker_is_stripped_and_detected() -> None:
    assert plain_markers("нарисуй краба /pixel") == (
        "нарисуй краба",
        False,
        False,
        True,
        False,
        False,
        False,
    )
    assert plain_markers("нарисуй схему /svg /pixel /c") == (
        "нарисуй схему",
        True,
        True,
        True,
        False,
        False,
        False,
    )
    assert plain_markers("https://example.com/pixel") == (
        "https://example.com/pixel",
        False,
        False,
        False,
        False,
        False,
        False,
    )


def test_pixel_plus_marker_keeps_the_original() -> None:
    assert plain_markers("нарисуй краба /pixel+") == (
        "нарисуй краба",
        False,
        False,
        False,
        True,
        False,
        False,
    )
    assert plain_markers("нарисуй краба /pixel+ /svg") == (
        "нарисуй краба",
        False,
        True,
        False,
        True,
        False,
        False,
    )
    assert plain_markers("https://example.com/pixel+") == (
        "https://example.com/pixel+",
        False,
        False,
        False,
        False,
        False,
        False,
    )


def test_pixel_test_marker_selects_the_native_mode() -> None:
    assert plain_markers("нарисуй краба /pixel-test") == (
        "нарисуй краба",
        False,
        False,
        False,
        False,
        True,
        False,
    )
    assert plain_markers("нарисуй краба /pixel-test /svg /c") == (
        "нарисуй краба",
        True,
        True,
        False,
        False,
        True,
        False,
    )
    assert plain_markers("https://example.com/pixel-test") == (
        "https://example.com/pixel-test",
        False,
        False,
        False,
        False,
        False,
        False,
    )


def test_pixel_test2_marker_selects_the_one_to_one_mode() -> None:
    assert plain_markers("нарисуй краба /pixel-test2") == (
        "нарисуй краба",
        False,
        False,
        False,
        False,
        False,
        True,
    )
    assert plain_markers("нарисуй краба /pixel-test2 /svg /c") == (
        "нарисуй краба",
        True,
        True,
        False,
        False,
        False,
        True,
    )
    assert plain_markers("https://example.com/pixel-test2") == (
        "https://example.com/pixel-test2",
        False,
        False,
        False,
        False,
        False,
        False,
    )


def test_pixel_test2_palette_suffix_is_parsed() -> None:
    assert split_end_markers("нарисуй краба /pixel-test2:gameboy") == (
        "нарисуй краба",
        False,
        False,
        False,
        False,
        False,
        True,
        "gameboy",
    )
    assert split_end_markers("нарисуй краба /pixel-test2:pico8 /svg") == (
        "нарисуй краба",
        False,
        True,
        False,
        False,
        False,
        True,
        "pico8",
    )
