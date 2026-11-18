from __future__ import annotations

import base64
from types import SimpleNamespace
from typing import Any

import discord

from discord_bot.discord_app import chat as chat_module
from discord_bot.discord_app.chat import ChatService
from discord_bot.discord_app.images import (
    MAX_IMAGES_PER_MESSAGE,
    attach_images,
    collect_image_parts,
)
from discord_bot.llm.errors import ErrorKind, LLMError
from discord_bot.memory.models import IngestResult


class FakeAttachment:
    def __init__(
        self,
        filename: str = "cat.png",
        content_type: str | None = "image/png",
        data: bytes = b"fake-png-bytes",
        *,
        size: int | None = None,
        error: Exception | None = None,
    ) -> None:
        self.filename = filename
        self.content_type = content_type
        self.size = len(data) if size is None else size
        self._data = data
        self._error = error

    async def read(self) -> bytes:
        if self._error is not None:
            raise self._error
        return self._data


def message_with(*attachments: FakeAttachment) -> SimpleNamespace:
    return SimpleNamespace(attachments=list(attachments))


def url_of(part: dict[str, Any]) -> str:
    return part["image_url"]["url"]


async def test_no_attachments_means_no_parts_and_no_note() -> None:
    parts, note = await collect_image_parts(message_with())

    assert parts == []
    assert note == ""


async def test_image_becomes_a_data_url_part() -> None:
    parts, note = await collect_image_parts(message_with(FakeAttachment(data=b"hello")))

    assert len(parts) == 1
    assert parts[0]["type"] == "image_url"
    url = url_of(parts[0])
    assert url.startswith("data:image/png;base64,")
    assert base64.b64decode(url.split(",", 1)[1]) == b"hello"
    assert note == "[изображение: cat.png]"


async def test_mime_type_falls_back_to_the_extension() -> None:
    parts, _ = await collect_image_parts(
        message_with(
            FakeAttachment(
                filename="photo.JPG",
                content_type="application/octet-stream",
            )
        )
    )

    assert url_of(parts[0]).startswith("data:image/jpeg;base64,")


async def test_non_image_attachments_are_ignored_silently() -> None:
    parts, note = await collect_image_parts(
        message_with(
            FakeAttachment(filename="doc.pdf", content_type="application/pdf")
        )
    )

    assert parts == []
    assert note == ""


async def test_oversized_image_is_skipped_with_a_note() -> None:
    parts, note = await collect_image_parts(
        message_with(FakeAttachment(size=20 * 1024 * 1024))
    )

    assert parts == []
    assert "cat.png" in note
    assert "слишком большой" in note


async def test_only_the_first_images_are_sent() -> None:
    attachments = [
        FakeAttachment(filename=f"{index}.png")
        for index in range(MAX_IMAGES_PER_MESSAGE + 1)
    ]

    parts, note = await collect_image_parts(message_with(*attachments))

    assert len(parts) == MAX_IMAGES_PER_MESSAGE
    assert "пропущено" in note
    assert f"{MAX_IMAGES_PER_MESSAGE}.png" in note


async def test_broken_download_does_not_fail_the_turn() -> None:
    parts, note = await collect_image_parts(
        message_with(FakeAttachment(error=RuntimeError("boom")))
    )

    assert parts == []
    assert "не удалось загрузить" in note


async def test_empty_file_is_skipped() -> None:
    parts, note = await collect_image_parts(message_with(FakeAttachment(data=b"")))

    assert parts == []
    assert "пустой файл" in note


def test_attach_images_updates_the_last_user_message() -> None:
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "что на картинке?"},
    ]

    attach_images(messages, [{"type": "image_url", "image_url": {"url": "data:..."}}])

    content = messages[-1]["content"]
    assert isinstance(content, list)
    assert content[0] == {"type": "text", "text": "что на картинке?"}
    assert content[1]["type"] == "image_url"


def test_attach_images_uses_a_placeholder_for_empty_text() -> None:
    messages = [{"role": "user", "content": "   "}]

    attach_images(messages, [{"type": "image_url", "image_url": {"url": "data:..."}}])

    assert messages[-1]["content"][0] == {
        "type": "text",
        "text": "Посмотри на приложенное изображение.",
    }


def test_attach_images_without_parts_keeps_the_content() -> None:
    messages = [{"role": "user", "content": "привет"}]

    attach_images(messages, [])

    assert messages[-1] == {"role": "user", "content": "привет"}


# --- one integration turn through ChatService ------------------------------


class FakeSettings:
    def __init__(self, values: dict[str, str] | None = None) -> None:
        self.values = values or {}

    async def get_all(self) -> dict[str, str]:
        return self.values


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
    def names(self) -> list[str]:
        return []

    def schemas(self, enabled: list[str]) -> list[dict[str, Any]]:
        return []

    async def execute(self, *args: Any, **kwargs: Any) -> str:
        return ""


class FakeLLM:
    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] | None = None
        self.image_messages: list[dict[str, Any]] | None = None
        self.fail_images = False

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
                    message=SimpleNamespace(content="Это кот.", tool_calls=None)
                )
            ]
        )

    async def complete_text(
        self,
        spec: Any,
        model: str,
        messages: list[dict[str, Any]],
        **kwargs: Any,
    ) -> str:
        if self.fail_images:
            raise LLMError(
                provider="gemini",
                model=model,
                status_code=503,
                kind=ErrorKind.provider_unavailable,
                detail="busy",
            )
        self.image_messages = messages
        return "На картинке кот."


def patch_providers(monkeypatch) -> None:
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


def dm_message(content: str = "что тут?") -> tuple[SimpleNamespace, list[str]]:
    sent: list[str] = []

    async def _reply(text: str, **kwargs: Any) -> None:
        sent.append(text)

    message = SimpleNamespace(
        id=43,
        content=content,
        channel=SimpleNamespace(
            type=discord.ChannelType.private, id=444, parent_id=None
        ),
        guild=None,
        author=SimpleNamespace(id=111, display_name="Аня", name="anya", bot=False),
        attachments=[FakeAttachment()],
        reply=_reply,
    )
    return message, sent


def channel_message(content: str = "смотрите") -> SimpleNamespace:
    async def _reply(text: str, **kwargs: Any) -> None:
        return None

    return SimpleNamespace(
        id=45,
        content=content,
        channel=SimpleNamespace(
            type=discord.ChannelType.text, id=555, parent_id=None
        ),
        guild=SimpleNamespace(id=777),
        author=SimpleNamespace(id=111, display_name="Аня", name="anya", bot=False),
        attachments=[FakeAttachment()],
        reply=_reply,
    )


async def test_chat_sends_the_image_and_stores_a_description(monkeypatch) -> None:
    memory = FakeMemory()
    llm = FakeLLM()
    bot = SimpleNamespace(
        user=SimpleNamespace(id=999, mentioned_in=lambda item: False)
    )
    chat = ChatService(bot, FakeSettings(), memory, llm, FakeRegistry())
    patch_providers(monkeypatch)
    message, sent = dm_message()

    await chat._handle(message)

    assert memory.ingested_content is not None
    assert memory.ingested_content.startswith(
        "[описание изображения: На картинке кот.]"
    )
    assert "[изображение: cat.png]" in memory.ingested_content
    assert "что тут?" in memory.ingested_content
    assert memory.recorded is not None
    assert sent == ["Это кот."]
    assert llm.image_messages is not None
    assert llm.messages is not None
    content = llm.messages[-1]["content"]
    assert isinstance(content, list)
    assert content[0]["text"] == memory.ingested_content
    assert content[1]["type"] == "image_url"


async def test_ambient_image_in_allowed_channel_is_described(
    monkeypatch,
) -> None:
    memory = FakeMemory()
    llm = FakeLLM()
    bot = SimpleNamespace(
        user=SimpleNamespace(id=999, mentioned_in=lambda item: False)
    )
    settings = FakeSettings({"allowed_channel_ids": '["555"]'})
    chat = ChatService(bot, settings, memory, llm, FakeRegistry())
    patch_providers(monkeypatch)

    await chat._handle(channel_message())

    assert memory.ingested_content is not None
    assert "описание изображения" in memory.ingested_content
    assert "На картинке кот." in memory.ingested_content
    assert llm.image_messages is not None
    assert llm.messages is None
    assert memory.recorded is None


async def test_image_memory_can_be_disabled(monkeypatch) -> None:
    memory = FakeMemory()
    llm = FakeLLM()
    bot = SimpleNamespace(
        user=SimpleNamespace(id=999, mentioned_in=lambda item: False)
    )
    settings = FakeSettings({"image_memory_enabled": "false"})
    chat = ChatService(bot, settings, memory, llm, FakeRegistry())
    patch_providers(monkeypatch)
    message, sent = dm_message()

    await chat._handle(message)

    assert memory.ingested_content == "что тут?\n[изображение: cat.png]"
    assert llm.image_messages is None
    assert memory.recorded is not None
    assert sent == ["Это кот."]


async def test_image_description_failure_does_not_break_the_turn(
    monkeypatch,
) -> None:
    memory = FakeMemory()
    llm = FakeLLM()
    llm.fail_images = True
    bot = SimpleNamespace(
        user=SimpleNamespace(id=999, mentioned_in=lambda item: False)
    )
    chat = ChatService(bot, FakeSettings(), memory, llm, FakeRegistry())
    patch_providers(monkeypatch)
    message, sent = dm_message()

    await chat._handle(message)

    assert memory.ingested_content == "что тут?\n[изображение: cat.png]"
    assert memory.recorded is not None
    assert sent == ["Это кот."]
