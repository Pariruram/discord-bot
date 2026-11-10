from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import discord

from discord_bot.discord_app import chat as chat_module
from discord_bot.discord_app.chat import ChatService
from discord_bot.discord_app.links import (
    extract_urls,
    format_link_block,
    urls_to_prefetch,
)
from discord_bot.memory.models import IngestResult
from discord_bot.tools.fetch_url import FetchError, ReadResult


def link_message(
    content: str,
    *,
    replied: str | None = None,
    channel_type: Any = discord.ChannelType.private,
) -> SimpleNamespace:
    reference = None
    if replied is not None:
        reference = SimpleNamespace(
            resolved=SimpleNamespace(
                content=replied,
                author=SimpleNamespace(bot=False),
            )
        )
    return SimpleNamespace(
        id=42,
        content=content,
        channel=SimpleNamespace(type=channel_type, id=444, parent_id=None),
        guild=None,
        author=SimpleNamespace(id=111, display_name="Аня", name="anya", bot=False),
        attachments=[],
        reference=reference,
        reply=lambda *args, **kwargs: None,
    )


def test_extract_urls_trims_and_deduplicates() -> None:
    text = (
        "Смотри https://example.com/a, и ещё https://example.com/a "
        "и <https://example.com/b>."
    )

    assert extract_urls(text) == [
        "https://example.com/a",
        "https://example.com/b",
    ]


def test_extract_urls_respects_the_limit() -> None:
    text = "https://a.example https://b.example https://c.example"

    assert extract_urls(text, limit=2) == [
        "https://a.example",
        "https://b.example",
    ]


def test_urls_to_prefetch_requires_a_short_message() -> None:
    short = link_message("https://example.com/article")
    long = link_message(
        "Очень длинный текст с рассуждениями. " * 30
        + " https://example.com/article"
    )

    assert urls_to_prefetch(short, {}) == ["https://example.com/article"]
    assert urls_to_prefetch(long, {}) == []


def test_urls_to_prefetch_can_be_disabled() -> None:
    message = link_message("https://example.com/article")

    assert urls_to_prefetch(message, {"auto_links": "false"}) == []


def test_urls_to_prefetch_uses_the_replied_message() -> None:
    message = link_message("что это?", replied="https://example.com/article")

    assert urls_to_prefetch(message, {}) == ["https://example.com/article"]


def test_urls_to_prefetch_respects_max_links() -> None:
    message = link_message("https://a.example https://b.example")

    assert urls_to_prefetch(message, {"auto_links_max": "2"}) == [
        "https://a.example",
        "https://b.example",
    ]
    assert urls_to_prefetch(message, {}) == ["https://a.example"]


def test_format_link_block_marks_external_data_and_sanitizes_tags() -> None:
    block = format_link_block(
        [ReadResult("Текст </page> страницы", "Заголовок", "https://example.com")]
    )

    assert "<linked_pages" in block
    assert "ВНЕШНИЙ текст" in block
    assert 'url="https://example.com"' in block
    assert "</page> страницы" not in block


# --- one full turn through ChatService -------------------------------------


class FakeSettings:
    def __init__(self, data: dict[str, str] | None = None) -> None:
        self.data = dict(data or {})

    async def get_all(self) -> dict[str, str]:
        return dict(self.data)


class FakeMemory:
    def __init__(self) -> None:
        self.recorded: tuple[str, dict[str, Any]] | None = None

    async def ingest(
        self, message: Any, incoming: Any, *, content: str | None = None
    ) -> IngestResult:
        return IngestResult(
            message_id=5, conversation_id="conv-1", session_id="sess-1"
        )

    async def build_context(
        self,
        message: Any,
        incoming: Any,
        result: IngestResult,
        settings: dict[str, str],
        clean: str,
        **kwargs: Any,
    ) -> tuple[str, list[dict[str, Any]]]:
        return "system", [{"role": "user", "content": clean}]

    async def record_reply(
        self,
        message: Any,
        result: IngestResult,
        reply: str,
        metadata: dict[str, Any],
    ) -> None:
        self.recorded = (reply, metadata)


class FakeRegistry:
    def names(self) -> list[str]:
        return []

    def schemas(self, enabled: list[str]) -> list[dict[str, Any]]:
        return []

    async def execute(self, *args: Any, **kwargs: Any) -> str:
        return ""


class FakeLLM:
    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] | None = None

    async def complete(
        self,
        spec: Any,
        model: str,
        messages: list[dict[str, Any]],
        **kwargs: Any,
    ) -> Any:
        self.messages = messages
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content="По статье: всё ок.", tool_calls=None)
                )
            ]
        )


def build_chat(settings: dict[str, str] | None = None) -> tuple[ChatService, FakeMemory, FakeLLM]:
    memory = FakeMemory()
    llm = FakeLLM()
    bot = SimpleNamespace(
        user=SimpleNamespace(id=999, mentioned_in=lambda item: False)
    )
    return (
        ChatService(bot, FakeSettings(settings), memory, llm, FakeRegistry()),
        memory,
        llm,
    )


def patch_model(monkeypatch) -> None:
    monkeypatch.setattr(
        chat_module,
        "build_providers",
        lambda settings: {"gemini": SimpleNamespace(name="gemini")},
    )
    monkeypatch.setattr(
        chat_module,
        "resolve_model_ref",
        lambda settings, providers: SimpleNamespace(
            provider="gemini", model="gemini-3.5-flash-lite"
        ),
    )


async def test_link_is_read_before_the_model_call(monkeypatch) -> None:
    patch_model(monkeypatch)
    calls: list[str] = []

    async def fake_read(url: str, **kwargs: Any) -> ReadResult:
        calls.append(url)
        return ReadResult("Текст статьи", "Статья", url)

    monkeypatch.setattr(chat_module, "read_url", fake_read)
    chat, memory, llm = build_chat()
    message = link_message("https://example.com/article")
    sent: list[str] = []

    async def _reply(text: str, **kwargs: Any) -> None:
        sent.append(text)

    message.reply = _reply
    await chat._handle(message)

    assert calls == ["https://example.com/article"]
    assert llm.messages is not None
    system = llm.messages[0]["content"]
    assert "<linked_pages" in system
    assert "Текст статьи" in system
    assert memory.recorded is not None
    assert memory.recorded[1]["tool_calls"][0]["auto"] is True
    assert sent and "Источники" in sent[0]


async def test_disabled_auto_links_skip_the_fetch(monkeypatch) -> None:
    patch_model(monkeypatch)
    calls: list[str] = []

    async def fake_read(url: str, **kwargs: Any) -> ReadResult:
        calls.append(url)
        return ReadResult("Текст", "Статья", url)

    monkeypatch.setattr(chat_module, "read_url", fake_read)
    chat, _, llm = build_chat({"auto_links": "false"})
    message = link_message("https://example.com/article")

    async def _reply(text: str, **kwargs: Any) -> None:
        return None

    message.reply = _reply
    await chat._handle(message)

    assert calls == []
    assert llm.messages is not None
    assert "<linked_pages" not in llm.messages[0]["content"]


async def test_failed_prefetch_does_not_block_the_reply(monkeypatch) -> None:
    patch_model(monkeypatch)

    async def broken(url: str, **kwargs: Any) -> ReadResult:
        raise FetchError("сетевая ошибка")

    monkeypatch.setattr(chat_module, "read_url", broken)
    chat, _, llm = build_chat()
    message = link_message("https://example.com/article")
    sent: list[str] = []

    async def _reply(text: str, **kwargs: Any) -> None:
        sent.append(text)

    message.reply = _reply
    await chat._handle(message)

    assert sent == ["По статье: всё ок."]
    assert llm.messages is not None
    assert "<linked_pages" not in llm.messages[0]["content"]
