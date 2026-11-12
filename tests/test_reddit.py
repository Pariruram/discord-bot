from __future__ import annotations

import html
from types import SimpleNamespace
from typing import Any

from discord_bot.discord_app.reddit import (
    find_reddit_url,
    parse_thread,
    thread_rss_url,
)
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


async def test_long_thread_is_summarized_in_chunks() -> None:
    llm = FakeLLM()
    text = "комментарий\n" * 6000

    summary = await summarize_thread(text, context(llm))

    assert summary == "Краткое обобщение."
    assert len(llm.calls) == 4


async def test_rss_failure_is_user_facing() -> None:
    class Broken:
        async def __call__(self, url: str, **kwargs: Any) -> FetchedPage:
            raise RuntimeError("403")

    tool = build_tool(Broken())

    result = await tool.handler(context(FakeLLM()), URL)

    assert "RSS недоступен" in result
