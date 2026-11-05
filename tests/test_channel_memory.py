from typing import Any

from discord_bot.storage.channel_memory import ChannelMemoryManager


class FakeRepo:
    def __init__(self, initial: dict[str, dict[str, Any]] | None = None) -> None:
        self.initial = initial or {}
        self.saved: dict[str, tuple[str, list[dict[str, str]]]] = {}

    async def load_channel_context(self, channel_id: str) -> dict[str, Any]:
        return self.initial.get(channel_id, {"summary": "", "messages": []})

    async def save_channel_context(
        self, channel_id: str, summary: str, messages: list[dict[str, str]]
    ) -> bool:
        self.saved[channel_id] = (summary, list(messages))
        return True


async def test_compaction_and_flush() -> None:
    repo = FakeRepo()
    manager = ChannelMemoryManager(
        repo, recent_limit=1, compact_every=1, max_message_chars=10
    )
    calls: list[tuple[str, list[dict[str, str]]]] = []

    async def summarize(old_summary: str, messages: list[dict[str, str]]) -> str:
        calls.append((old_summary, messages))
        return "summary"

    await manager.add_message("c", "alice", "one", summarize=summarize)
    await manager.add_message("c", "alice", "two", summarize=summarize)
    await manager.add_message("c", "alice", "three", summarize=summarize)

    assert len(calls) == 1
    assert [item["content"] for item in calls[0][1]] == ["one", "two"]

    context = await manager.get_context("c")
    assert "summary" in context
    assert "three" in context

    await manager.flush_all()
    summary, messages = repo.saved["c"]
    assert summary == "summary"
    assert [item["content"] for item in messages] == ["three"]


async def test_failed_summarization_does_not_lose_messages() -> None:
    repo = FakeRepo()
    manager = ChannelMemoryManager(
        repo, recent_limit=1, compact_every=1, max_message_chars=20
    )

    async def broken(old_summary: str, messages: list[dict[str, str]]) -> str:
        raise RuntimeError("llm down")

    await manager.add_message("c", "alice", "one", summarize=broken)
    await manager.add_message("c", "alice", "two", summarize=broken)
    await manager.add_message("c", "alice", "three", summarize=broken)

    context = await manager.get_context("c")
    assert "one" in context
    assert "two" in context
    assert "three" in context


async def test_context_is_loaded_from_repository() -> None:
    repo = FakeRepo(
        {
            "c": {
                "summary": "old summary",
                "messages": [{"author": "bob", "content": "hello"}],
            }
        }
    )
    manager = ChannelMemoryManager(repo)

    context = await manager.get_context("c")
    assert "old summary" in context
    assert "bob: hello" in context


async def test_long_messages_are_truncated() -> None:
    repo = FakeRepo()
    manager = ChannelMemoryManager(repo, max_message_chars=5)
    await manager.add_message("c", "alice", "abcdefghij")
    context = await manager.get_context("c")
    assert "abcd…" in context
