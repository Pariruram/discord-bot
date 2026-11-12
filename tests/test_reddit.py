from __future__ import annotations

import html
from types import SimpleNamespace
from typing import Any

import pytest

from discord_bot.discord_app.reddit import (
    find_reddit_url,
    parse_thread,
    thread_label,
    thread_rss_url,
    thread_rss_urls,
)
from discord_bot.llm.errors import ErrorKind, LLMError
from discord_bot.tools import read_thread as read_thread_module
from discord_bot.tools.base import ToolContext
from discord_bot.tools.fetch_url import FetchedPage
from discord_bot.tools.read_thread import build_tool, summarize_thread

URL = (
    "https://www.reddit.com/r/aigamedev/comments/1xsxna4/"
    "heres_a_good_one_for_the_code_is_okay_but_you/"
)


def atom_feed() -> str:
    post = html.escape("<p>Пост про Saltline и код.</p>")
    comment = html.escape("<p>Комментарий про арт.</p>")
    deleted = html.escape("<p>[deleted]</p>")
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Тред про Saltline</title>
  <entry>
    <author><name>/u/OP</name></author>
    <title>Тред про Saltline</title>
    <content type="html">{post}</content>
  </entry>
  <entry>
    <author><name>/u/Artist</name></author>
    <content type="html">{comment}</content>
  </entry>
  <entry>
    <author><name>/u/Ghost</name></author>
    <content type="html">{deleted}</content>
  </entry>
</feed>"""


class FakeFetcher:
    def __init__(self, text: str = "") -> None:
        self.text = text or atom_feed()
        self.calls: list[str] = []

    async def __call__(self, url: str, **kwargs: Any) -> FetchedPage:
        self.calls.append(url)
        return FetchedPage(
            url=url,
            status=200,
            content_type="application/atom+xml",
            text=self.text,
        )


class HostFailingFetcher:
    """Succeeds for every host except the one named in ``failing_host``."""

    def __init__(self, failing_host: str) -> None:
        self.failing_host = failing_host
        self.calls: list[str] = []

    async def __call__(self, url: str, **kwargs: Any) -> FetchedPage:
        self.calls.append(url)
        if self.failing_host in url:
            raise RuntimeError("страница вернула HTTP 429")
        return FetchedPage(
            url=url,
            status=200,
            content_type="application/atom+xml",
            text=atom_feed(),
        )


class BrokenCounter:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def __call__(self, url: str, **kwargs: Any) -> FetchedPage:
        self.calls.append(url)
        raise RuntimeError("страница вернула HTTP 429")


class FakeExtractor:
    def __init__(self, text: str = "", error: Exception | None = None) -> None:
        self.text = text
        self.error = error
        self.calls: list[str] = []

    async def __call__(self, url: str) -> str:
        self.calls.append(url)
        if self.error is not None:
            raise self.error
        return self.text


class FakeLLM:
    def __init__(self, summary: str = "Краткое обобщение.") -> None:
        self.summary = summary
        self.calls: list[list[dict[str, str]]] = []

    async def complete_text(
        self,
        provider: Any,
        model: str,
        messages: list[dict[str, str]],
        **kwargs: Any,
    ) -> str:
        self.calls.append(messages)
        return self.summary


class FailingLLM:
    """Echoes the prompt on the final call and can fail selected calls."""

    def __init__(
        self,
        fail_calls: set[int] | None = None,
        *,
        kind: ErrorKind = ErrorKind.provider_unavailable,
    ) -> None:
        self.fail_calls = fail_calls or set()
        self.kind = kind
        self.calls: list[dict[str, Any]] = []

    async def complete_text(
        self,
        provider: Any,
        model: str,
        messages: list[dict[str, str]],
        **kwargs: Any,
    ) -> str:
        self.calls.append(
            {"messages": messages, "purpose": kwargs.get("purpose", "")}
        )
        if len(self.calls) in self.fail_calls:
            unavailable = self.kind is ErrorKind.provider_unavailable
            raise LLMError(
                provider="gemini",
                model=model,
                status_code=503 if unavailable else 400,
                kind=self.kind,
                detail="busy" if unavailable else "bad request",
            )
        purpose = str(kwargs.get("purpose") or "")
        if purpose.startswith("read_thread_final"):
            return messages[-1]["content"][:500]
        return f"part-{len(self.calls)}"


def context(llm: Any) -> ToolContext:
    return ToolContext(
        user_id="1",
        channel_id="2",
        llm=llm,
        provider=SimpleNamespace(name="gemini", default_model="x"),
        model="gemini-3.5-flash-lite",
    )


def test_reddit_url_and_rss_conversion() -> None:
    assert find_reddit_url(f"смотри {URL}") == URL
    assert find_reddit_url("https://example.com/r/x/comments/1") is None
    assert thread_rss_url(URL) == (
        "https://www.reddit.com/r/aigamedev/comments/1xsxna4/.rss"
    )
    assert thread_rss_urls(URL) == [
        "https://www.reddit.com/r/aigamedev/comments/1xsxna4/.rss",
        "https://www.redditmedia.com/r/aigamedev/comments/1xsxna4/.rss",
    ]
    assert thread_label(URL).startswith("r/aigamedev: heres a good one")


def test_parse_feed_extracts_post_and_comments() -> None:
    thread = parse_thread(atom_feed(), URL)

    assert thread.title == "Тред про Saltline"
    assert thread.author == "/u/OP"
    assert "Saltline" in thread.post_text
    assert len(thread.comments) == 1
    assert thread.comments[0][0] == "/u/Artist"
    assert "арт" in thread.comments[0][1]

    text = thread.as_text()
    assert "Пост: Тред про Saltline" in text
    assert "[1] /u/Artist:" in text
    assert "Ghost" not in text


async def test_read_thread_summarizes_and_cites() -> None:
    llm = FakeLLM("Тред про то, что игра целиком на коде.")
    fetcher = FakeFetcher()
    tool = build_tool(fetcher)
    ctx = context(llm)

    result = await tool.handler(ctx, URL)

    assert "Комментариев: 1" in result
    assert "Тред про то" in result
    assert ctx.citations[0].url == URL
    assert fetcher.calls[0].endswith("/.rss")
    assert len(llm.calls) == 1


async def test_rss_falls_back_to_media_host() -> None:
    fetcher = HostFailingFetcher("www.reddit.com")
    llm = FakeLLM("Краткое обобщение.")
    ctx = context(llm)

    result = await build_tool(fetcher, min_interval=0).handler(ctx, URL)

    assert "Краткое обобщение." in result
    assert fetcher.calls[0].startswith("https://www.reddit.com/")
    assert fetcher.calls[1].startswith("https://www.redditmedia.com/")
    assert ctx.citations[0].url == URL


async def test_cached_thread_skips_the_second_fetch() -> None:
    fetcher = FakeFetcher()
    llm = FakeLLM("Краткое обобщение.")
    tool = build_tool(fetcher, min_interval=0)
    ctx = context(llm)

    await tool.handler(ctx, URL)
    await tool.handler(ctx, URL)

    assert len(fetcher.calls) == 1
    assert len(llm.calls) == 2


async def test_recent_failure_is_not_retried() -> None:
    fetcher = BrokenCounter()
    tool = build_tool(fetcher, min_interval=0, failure_ttl=60)
    ctx = context(FakeLLM())

    first = await tool.handler(ctx, URL)
    second = await tool.handler(ctx, URL)

    assert "RSS недоступен" in first
    assert "RSS недоступен" in second
    # Two hosts on the first attempt, zero requests during the failure TTL.
    assert len(fetcher.calls) == 2


async def test_tavily_fallback_when_rss_fails() -> None:
    fetcher = BrokenCounter()
    extractor = FakeExtractor("Обсуждение " * 300)
    tool = build_tool(
        fetcher, extractor=extractor, min_interval=0, failure_ttl=0
    )
    ctx = context(FakeLLM("Краткое обобщение."))

    result = await tool.handler(ctx, URL)

    assert "Краткое обобщение." in result
    assert "Tavily Extract" in result
    assert extractor.calls == [URL]
    assert len(fetcher.calls) == 2


async def test_tavily_short_text_is_rejected() -> None:
    fetcher = BrokenCounter()
    extractor = FakeExtractor("слишком коротко")
    tool = build_tool(
        fetcher, extractor=extractor, min_interval=0, failure_ttl=0
    )

    result = await tool.handler(context(FakeLLM()), URL)

    assert "RSS недоступен" in result
    assert extractor.calls == [URL]


async def test_tavily_extract_error_falls_back_to_message() -> None:
    fetcher = BrokenCounter()
    extractor = FakeExtractor(error=RuntimeError("403"))
    tool = build_tool(
        fetcher, extractor=extractor, min_interval=0, failure_ttl=0
    )

    result = await tool.handler(context(FakeLLM()), URL)

    assert "RSS недоступен" in result
    assert extractor.calls == [URL]


async def test_tavily_text_is_cached_between_calls() -> None:
    fetcher = BrokenCounter()
    extractor = FakeExtractor("Обсуждение " * 300)
    tool = build_tool(
        fetcher, extractor=extractor, min_interval=0, failure_ttl=0
    )
    ctx = context(FakeLLM("Краткое обобщение."))

    await tool.handler(ctx, URL)
    await tool.handler(ctx, URL)

    assert extractor.calls == [URL]
    assert len(fetcher.calls) == 2


async def test_long_thread_is_summarized_in_chunks() -> None:
    llm = FakeLLM()
    text = "комментарий\n" * 6000

    summary = await summarize_thread(text, context(llm))

    assert summary == "Краткое обобщение."
    assert len(llm.calls) == 4


async def test_partial_chunk_failure_keeps_other_parts(monkeypatch) -> None:
    monkeypatch.setattr(read_thread_module, "RETRY_DELAY_SECONDS", 0)
    llm = FailingLLM({2, 3})

    summary = await summarize_thread(
        "комментарий\n" * 6000, context(llm)
    )

    assert "Часть 1" in summary
    assert "Часть 3" in summary
    assert "Не обработаны части: 2 из 3" in summary
    assert "Не обработана часть 2 из 3" in summary
    assert len(llm.calls) == 5


async def test_all_chunks_failing_raises(monkeypatch) -> None:
    monkeypatch.setattr(read_thread_module, "RETRY_DELAY_SECONDS", 0)
    llm = FailingLLM({1, 2, 3, 4, 5, 6})

    with pytest.raises(LLMError):
        await summarize_thread("комментарий\n" * 6000, context(llm))

    assert len(llm.calls) == 6


async def test_provider_overload_is_retried_once(monkeypatch) -> None:
    monkeypatch.setattr(read_thread_module, "RETRY_DELAY_SECONDS", 0)
    llm = FailingLLM({1})

    summary = await summarize_thread("короткий тред", context(llm))

    assert summary == "короткий тред"
    assert len(llm.calls) == 2
    assert llm.calls[1]["purpose"] == "read_thread_final_retry"


async def test_non_overload_error_is_not_retried(monkeypatch) -> None:
    monkeypatch.setattr(read_thread_module, "RETRY_DELAY_SECONDS", 0)
    llm = FailingLLM({1}, kind=ErrorKind.bad_request)

    with pytest.raises(LLMError):
        await summarize_thread("короткий тред", context(llm))

    assert len(llm.calls) == 1


async def test_tool_cites_thread_when_summary_fails(monkeypatch) -> None:
    monkeypatch.setattr(read_thread_module, "RETRY_DELAY_SECONDS", 0)
    ctx = context(FailingLLM({1, 2}))

    result = await build_tool(FakeFetcher()).handler(ctx, URL)

    assert "URL:" in result
    assert "read_thread повторно" in result
    assert ctx.citations[0].url == URL
    assert ctx.citations[0].title == "Тред про Saltline"


async def test_rss_failure_is_user_facing() -> None:
    class Broken:
        async def __call__(self, url: str, **kwargs: Any) -> FetchedPage:
            raise RuntimeError("403")

    tool = build_tool(Broken())

    result = await tool.handler(context(FakeLLM()), URL)

    assert "RSS недоступен" in result


async def test_rss_failure_still_cites_the_thread() -> None:
    class Broken:
        async def __call__(self, url: str, **kwargs: Any) -> FetchedPage:
            raise RuntimeError("429")

    ctx = context(FakeLLM())

    result = await build_tool(Broken()).handler(ctx, URL)

    assert "web_search" in result
    assert ctx.citations[0].url == URL
    assert ctx.citations[0].title.startswith("r/aigamedev")
