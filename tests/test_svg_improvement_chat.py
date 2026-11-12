from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import discord

from discord_bot.citations import Citation
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
        return ["draw_svg", "read_thread"]

    def schemas(self, enabled: list[str]) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": name,
                    "parameters": {"type": "object", "properties": {}},
                },
            }
            for name in ("draw_svg", "read_thread")
            if name in enabled
        ]

    async def execute(self, name: str, raw_args: str, context: Any) -> str:
        self.contexts.append(context)
        return "drawn"


def tool_call(
    name: str = "draw_svg", arguments: str = '{"prompt": "улучши"}'
) -> SimpleNamespace:
    return SimpleNamespace(
        id="call-1",
        function=SimpleNamespace(name=name, arguments=arguments),
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


async def test_pixel_grid_and_palette_reach_the_tool_context(monkeypatch) -> None:
    chat, _, llm, registry = build(
        monkeypatch, [response(tool_calls=[tool_call()]), response(content="Готово")]
    )

    await chat._handle(dm("нарисуй краба /pixel 64:gameboy"))

    assert llm.messages is not None
    assert "pixel_grid=64" in llm.messages[0]["content"]
    assert "palette=gameboy" in llm.messages[0]["content"]
    context = registry.contexts[0]
    assert context.force_pixel_art is True
    assert context.force_pixel_grid == 64
    assert context.pixel_palette == "gameboy"


async def test_reddit_link_forces_read_thread(monkeypatch) -> None:
    url = (
        "https://www.reddit.com/r/aigamedev/comments/1xsxna4/"
        "heres_a_good_one_for_the_code_is_okay_but_you/"
    )
    chat, memory, llm, registry = build(
        monkeypatch,
        [
            response(
                tool_calls=[
                    tool_call("read_thread", json.dumps({"url": url}))
                ]
            ),
            response(content="Итог треда."),
        ],
    )

    await chat._handle(dm(url))

    assert llm.calls[0]["tool_choice"] == {
        "type": "function",
        "function": {"name": "read_thread"},
    }
    assert "thread_request" in llm.messages[0]["content"]
    assert memory.recorded is not None


async def test_reddit_source_stays_first_when_the_tool_fails(monkeypatch) -> None:
    url = (
        "https://www.reddit.com/r/aigamedev/comments/1xsxna4/"
        "heres_a_good_one_for_the_code_is_okay_but_you/"
    )
    chat, memory, _, _ = build(
        monkeypatch,
        [
            response(
                tool_calls=[
                    tool_call("read_thread", json.dumps({"url": url}))
                ]
            ),
            response(content="Не удалось обобщить тред."),
        ],
    )

    class FailingRegistry(FakeRegistry):
        async def execute(self, name, raw_args, context):
            self.contexts.append(context)
            if name == "read_thread":
                context.citations.append(
                    Citation(
                        title="Search result",
                        url="https://example.com/search",
                    )
                )
                return "Не удалось прочитать Reddit-тред: RSS недоступен."
            return "drawn"

    chat.registry = FailingRegistry()
    sent: list[str] = []
    message = dm(url)

    async def capture(text: str, **kwargs: Any) -> None:
        sent.append(text)

    message.reply = capture

    await chat._handle(message)

    assert memory.recorded is not None
    reply, metadata = memory.recorded
    urls = [item["url"] for item in metadata["citations"]]
    assert urls == [url, "https://example.com/search"]
    assert url in sent[0]
    assert sent[0].index(url) < sent[0].index("https://example.com/search")
    assert reply == "Не удалось обобщить тред."
