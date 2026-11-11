from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import discord

from discord_bot.discord_app import chat as chat_module
from discord_bot.discord_app.chat import ChatService
from discord_bot.memory.models import IngestResult

SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10">'
    '<rect width="10" height="10"/>'
    "</svg>"
)


class FakeAttachment:
    filename = "source.svg"
    content_type = "image/svg+xml"
    size = len(SVG)

    async def read(self) -> bytes:
        return SVG.encode()


class FakeSettings:
    async def get_all(self) -> dict[str, str]:
        return {}


class FakeMemory:
    def __init__(self) -> None:
        self.ingested_content: str | None = None
        self.recorded: tuple[str, dict[str, Any]] | None = None

    async def ingest(
        self, message: Any, incoming: Any, *, content: str | None = None
    ) -> IngestResult:
        self.ingested_content = content
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
    def __init__(self) -> None:
        self.contexts: list[Any] = []

    def names(self) -> list[str]:
        return ["draw_svg"]

    def schemas(self, enabled: list[str]) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": "draw_svg",
                    "description": "draw",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ]

    async def execute(self, name: str, raw_args: str, context: Any) -> str:
        self.contexts.append(context)
        return "drawn"


def tool_call() -> SimpleNamespace:
    return SimpleNamespace(
        id="call-1",
        function=SimpleNamespace(name="draw_svg", arguments='{"prompt": "улучши"}'),
    )


def response(content: str | None = None, tool_calls: list | None = None):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content=content, tool_calls=tool_calls)
            )
        ]
    )


class FakeLLM:
    def __init__(self, responses: list[Any]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []
        self.messages: list[dict[str, Any]] | None = None

    async def complete(self, spec, model, messages, **kwargs):
        self.calls.append(dict(kwargs))
        self.messages = messages
        return self.responses.pop(0)


def dm(content: str) -> SimpleNamespace:
    message = SimpleNamespace(
        id=42,
        content=content,
        channel=SimpleNamespace(
            type=discord.ChannelType.private, id=444, parent_id=None
        ),
        guild=None,
        author=SimpleNamespace(id=111, display_name="Аня", name="anya", bot=False),
        attachments=[FakeAttachment()],
    )

    async def reply(text: str, **kwargs: Any) -> None:
        return None

    message.reply = reply
    return message


def build(
    monkeypatch, responses: list[Any]
) -> tuple[ChatService, FakeMemory, FakeLLM, FakeRegistry]:
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
    memory = FakeMemory()
    llm = FakeLLM(responses)
    registry = FakeRegistry()
    bot = SimpleNamespace(
        user=SimpleNamespace(id=999, mentioned_in=lambda item: False)
    )
    chat = ChatService(bot, FakeSettings(), memory, llm, registry)
    return chat, memory, llm, registry


async def test_svg_attachment_is_validated_and_forced_into_the_tool(
    monkeypatch,
) -> None:
    chat, memory, llm, registry = build(
        monkeypatch, [response(tool_calls=[tool_call()]), response(content="Готово")]
    )

    await chat._handle(dm("улучши этот svg"))

    assert memory.ingested_content is not None
    assert "[svg-вложение: source.svg]" in memory.ingested_content
    assert "<svg" not in memory.ingested_content
    assert llm.calls[0]["tool_choice"] == {
        "type": "function",
        "function": {"name": "draw_svg"},
    }
    assert llm.messages is not None
    assert "Пользователь приложил SVG-исходник" in llm.messages[0]["content"]
    assert registry.contexts[0].source_svg == SVG
    assert memory.recorded is not None


async def test_question_about_the_svg_does_not_force_a_redraw(monkeypatch) -> None:
    chat, _, llm, registry = build(
        monkeypatch, [response(content="Это простая схема.")]
    )

    await chat._handle(dm("что это за svg?"))

    assert llm.calls[0]["tool_choice"] == "auto"
    assert registry.contexts == []


async def test_pixel_marker_reaches_the_tool_context(monkeypatch) -> None:
    chat, _, llm, registry = build(
        monkeypatch, [response(tool_calls=[tool_call()]), response(content="Готово")]
    )

    await chat._handle(dm("нарисуй краба /pixel"))

    assert llm.calls[0]["tool_choice"] == {
        "type": "function",
        "function": {"name": "draw_svg"},
    }
    assert llm.messages is not None
    assert "pixel_art=true" in llm.messages[0]["content"]
    assert registry.contexts[0].force_pixel_art is True


async def test_pixel_plus_marker_keeps_the_original(monkeypatch) -> None:
    chat, _, llm, registry = build(
        monkeypatch, [response(tool_calls=[tool_call()]), response(content="Готово")]
    )

    await chat._handle(dm("нарисуй краба /pixel+"))

    assert llm.messages is not None
    assert "оригинальный PNG" in llm.messages[0]["content"]
    assert registry.contexts[0].force_pixel_plus is True


async def test_pixel_test_marker_selects_native_mode(monkeypatch) -> None:
    chat, _, llm, registry = build(
        monkeypatch, [response(tool_calls=[tool_call()]), response(content="Готово")]
    )

    await chat._handle(dm("нарисуй краба /pixel-test"))

    assert llm.messages is not None
    assert "оригинальный PNG" in llm.messages[0]["content"]
    assert registry.contexts[0].force_pixel_test is True
