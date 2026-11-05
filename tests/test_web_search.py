from typing import Any

from bot.tools.base import ToolContext
from bot.tools.web_search import build_tool


def context() -> ToolContext:
    return ToolContext(user_id="1", channel_id="2")


async def test_search_results_include_urls_and_citations() -> None:
    async def search(**kwargs: Any) -> dict[str, Any]:
        assert kwargs["max_results"] == 3
        return {
            "results": [
                {
                    "title": "First",
                    "url": "https://example.com/a",
                    "content": "First content",
                },
                {
                    "title": "",
                    "url": "https://example.com/b",
                    "content": "Second content",
                },
            ]
        }

    tool = build_tool(search)
    ctx = context()
    result = await tool.handler(ctx, query="test")

    assert "[1] First" in result
    assert "https://example.com/a" in result
    assert "First content" in result
    assert "Second content" in result

    assert [citation.url for citation in ctx.citations] == [
        "https://example.com/a",
        "https://example.com/b",
    ]
    assert ctx.citations[1].title == "https://example.com/b"


async def test_empty_results_have_no_citations() -> None:
    async def search(**kwargs: Any) -> dict[str, Any]:
        return {"results": []}

    tool = build_tool(search)
    ctx = context()
    result = await tool.handler(ctx, query="test")

    assert result == "Поиск не дал результатов."
    assert ctx.citations == []


async def test_missing_api_key_is_reported() -> None:
    tool = build_tool(None)
    ctx = context()
    result = await tool.handler(ctx, query="test")

    assert "не настроен" in result
    assert ctx.citations == []


async def test_non_http_urls_are_not_cited() -> None:
    async def search(**kwargs: Any) -> dict[str, Any]:
        return {
            "results": [
                {
                    "title": "Local",
                    "url": "ftp://example.com/file",
                    "content": "Content",
                }
            ]
        }

    tool = build_tool(search)
    ctx = context()
    result = await tool.handler(ctx, query="test")

    assert "Content" in result
    assert ctx.citations == []


async def test_non_dict_results_are_skipped() -> None:
    async def search(**kwargs: Any) -> dict[str, Any]:
        return {"results": ["not a dict"]}

    tool = build_tool(search)
    ctx = context()
    result = await tool.handler(ctx, query="test")

    assert result == "Поиск не дал результатов."
    assert ctx.citations == []
