from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from discord_bot.tools.base import ToolContext
from discord_bot.tools.web_search import CONTENT_CHARS, build_tool


class FakeSearch:
    def __init__(self, results: list[dict[str, Any]] | None = None) -> None:
        self.results = results if results is not None else [result()]
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return {"results": self.results}


def result(
    *,
    title: str = "Источник",
    url: str = "https://example.com/source",
    content: str = "Полезный текст",
    published: str | None = "2026-11-01",
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "title": title,
        "url": url,
        "content": content,
    }
    if published is not None:
        item["published_date"] = published
    return item


def context(**settings: str) -> ToolContext:
    return ToolContext(user_id="1", channel_id="2", settings=settings)


async def test_default_search_shape() -> None:
    search = FakeSearch()
    tool = build_tool(search)

    response = await tool.handler(context(), "ai news")

    assert "Источник" in response
    assert "Дата: 2026-11-01" in response
    call = search.calls[0]
    assert call["query"] == "ai news"
    assert call["max_results"] == 5
    assert call["search_depth"] == "basic"
    assert call["topic"] == "general"
    assert call["include_published_date"] is True
    assert call["chunks_per_source"] == 2
    assert "time_range" not in call
    assert "filter_by_published_date" not in call


async def test_default_result_count_comes_from_settings() -> None:
    search = FakeSearch()

    await build_tool(search).handler(
        context(search_max_results="7"), "новости"
    )

    assert search.calls[0]["max_results"] == 7


async def test_explicit_count_and_date_range_are_passed() -> None:
    search = FakeSearch()

    await build_tool(search).handler(
        context(),
        "выборы",
        max_results=8,
        time_range="week",
        topic="news",
        deep=True,
    )

    call = search.calls[0]
    assert call["max_results"] == 8
    assert call["time_range"] == "week"
    assert call["topic"] == "news"
    assert call["search_depth"] == "advanced"
    assert call["filter_by_published_date"] is True


async def test_exact_dates_are_passed_and_filtered() -> None:
    search = FakeSearch()

    await build_tool(search).handler(
        context(),
        "релиз",
        start_date="2026-10-01",
        end_date="2026-10-31",
    )

    call = search.calls[0]
    assert call["start_date"] == "2026-10-01"
    assert call["end_date"] == "2026-10-31"
    assert call["filter_by_published_date"] is True


async def test_count_is_clamped() -> None:
    search = FakeSearch()
    tool = build_tool(search)

    await tool.handler(context(), "x", max_results=50)
    await tool.handler(context(), "x", max_results=0)

    assert search.calls[0]["max_results"] == 10
    assert search.calls[1]["max_results"] == 1


@pytest.mark.parametrize(
    "kwargs",
    [
        {"start_date": "2026-13-01"},
        {"start_date": "2026-10-31", "end_date": "2026-10-01"},
        {"time_range": "week", "start_date": "2026-10-01"},
        {"time_range": "fortnight"},
        {"topic": "sports"},
    ],
)
async def test_invalid_arguments_do_not_call_search(kwargs: dict) -> None:
    search = FakeSearch()

    response = await build_tool(search).handler(context(), "x", **kwargs)

    assert "Поиск не выполнен" in response
    assert search.calls == []


async def test_long_content_is_truncated() -> None:
    search = FakeSearch([result(content="я" * 5000)])

    response = await build_tool(search).handler(context(), "x")

    assert len(response) < 1500
    assert response.endswith("…")


async def test_empty_results_are_reported() -> None:
    search = FakeSearch([])

    response = await build_tool(search).handler(context(), "x")

    assert response == "Поиск не дал результатов."


async def test_missing_search_backend_is_reported() -> None:
    response = await build_tool(None).handler(context(), "x")

    assert "не настроен" in response


async def test_blank_query_is_rejected() -> None:
    search = FakeSearch()

    response = await build_tool(search).handler(context(), "   ")

    assert "пустой запрос" in response
    assert search.calls == []


async def test_site_only_query_is_rejected_before_search() -> None:
    search = FakeSearch()

    response = await build_tool(search).handler(
        context(), "site:reddit.com site:old.reddit.com"
    )

    assert "только из site:" in response
    assert search.calls == []


async def test_search_backend_error_is_user_facing() -> None:
    class Broken:
        async def __call__(self, **kwargs: Any) -> dict[str, Any]:
            raise RuntimeError(
                "Query cannot consist only of site: operators."
            )

    response = await build_tool(Broken()).handler(context(), "reddit thread")

    assert "Поиск не выполнен" in response
    assert "site:" in response


async def test_multiple_stale_years_in_a_news_query_are_rejected() -> None:
    search = FakeSearch()
    current = datetime.now(UTC).year

    response = await build_tool(search).handler(
        context(), f"AI news November 11 {current - 2} OR {current - 1}"
    )

    assert "Поиск не выполнен" in response
    assert str(current) in response
    assert "time_range" in response
    assert search.calls == []


async def test_explicit_historical_year_is_allowed() -> None:
    search = FakeSearch()
    current = datetime.now(UTC).year

    await build_tool(search).handler(
        context(), f"новости за {current - 1}"
    )

    assert search.calls
    assert str(current - 1) in search.calls[0]["query"]


async def test_current_year_or_date_filter_bypasses_the_guard() -> None:
    search = FakeSearch()
    current = datetime.now(UTC).year

    await build_tool(search).handler(
        context(), f"Jensen Huang news {current}"
    )
    await build_tool(search).handler(
        context(),
        f"AI news November 11 {current - 2} OR {current - 1}",
        time_range="week",
    )

    assert len(search.calls) == 2


def test_schema_exposes_count_and_date_filters() -> None:
    tool = build_tool(FakeSearch())
    properties = tool.parameters["properties"]

    assert properties["max_results"]["maximum"] == 10
    assert set(properties["time_range"]["enum"]) == {
        "day",
        "week",
        "month",
        "year",
    }
    assert properties["topic"]["enum"] == ["general", "news"]
    assert properties["deep"]["default"] is False
    assert CONTENT_CHARS == 700
