"""Tavily web search tool with dates, source count and citations."""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from discord_bot.citations import Citation
from discord_bot.tools.base import Tool, ToolContext
from discord_bot.utils import safe_int, truncate

logger = logging.getLogger(__name__)

SearchFn = Callable[..., Awaitable[dict[str, Any]]]

DEFAULT_RESULTS = 5
MAX_RESULTS = 10
CONTENT_CHARS = 700
TIME_RANGES = {"day", "week", "month", "year"}

DESCRIPTION = (
    "Точный веб-поиск для свежих фактов, новостей, цен, дат, событий после "
    "2024 года и проверки утверждений. Используй, когда ответ требует "
    "актуальных данных или источников; НЕ используй для общих знаний, кода, "
    "рассуждений и бытовых советов.\n"
    "Для фактчека: сформулируй в query проверяемое утверждение с ключевыми "
    "сущностями и датами, запроси 3–5 источников и сравни первоисточники. В "
    "ответе отдели подтверждённое от неподтверждённого и обязательно дай "
    "ссылки.\n"
    "Запрос — 2–6 ключевых слов без вводных слов; точные названия и цитаты "
    "заключай в кавычки. Год в query — только если пользователь явно назвал "
    "год или период. Для свежести вместо года используй topic=news и "
    "time_range=week/month. Если пользователь называет день/месяц без года — "
    "бери текущий год из <current_datetime> и передавай start_date/end_date. "
    "deep=true — только по явной просьбе о глубоком исследовании (дороже, "
    "2 кредита)."
)

_YEAR_RE = re.compile(r"\b20\d{2}\b")
_HISTORICAL_RE = re.compile(
    r"\b(за|в|с|по|during|in|since|until)\s+20\d{2}\b|"
    r"\b20\d{2}\s*(год|году|года|year)\b|архив|history|сравн|динамик",
    re.IGNORECASE,
)


def _format_result(index: int, item: dict[str, Any]) -> str:
    title = str(item.get("title") or "").strip()
    url = str(item.get("url") or "").strip()
    content = truncate(str(item.get("content") or "").strip(), CONTENT_CHARS)
    published = str(item.get("published_date") or "").strip()

    parts: list[str] = []
    if title:
        parts.append(f"[{index}] {title}")
    if url:
        parts.append(url)
    if published:
        parts.append(f"Дата: {published}")
    if content:
        parts.append(content)
    return "\n".join(parts)


def _parse_date(value: str | None, field: str) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        datetime.strptime(text, "%Y-%m-%d")
    except ValueError as exc:
        raise ValueError(f"{field} должен быть в формате YYYY-MM-DD") from exc
    return text


def _only_site_operators(query: str) -> bool:
    """Tavily rejects queries that contain nothing but site: filters."""

    tokens = query.split()
    return bool(tokens) and all(
        token.lower().startswith("site:") and len(token) > len("site:")
        for token in tokens
    )


def _stale_date_error(query: str) -> str | None:
    """Reject obviously stale years in a news query before spending a credit."""

    years = sorted(set(_YEAR_RE.findall(query)))
    if not years:
        return None
    now = datetime.now(UTC)
    if str(now.year) in years:
        return None
    if _HISTORICAL_RE.search(query):
        return None
    if len(years) < 2:
        return None
    return (
        f"Поиск не выполнен: сейчас {now.strftime('%Y-%m-%d')}, а в запросе "
        f"годы {', '.join(years)}. Убери годы из query: для свежих новостей "
        "используй topic=news и time_range=week/month, для точной даты — "
        f"start_date/end_date в {now.year} году."
    )


def build_tool(search: SearchFn | None) -> Tool:
    """Build the tool with an injected search callable (for tests and DI)."""

    async def web_search(
        context: ToolContext,
        query: str,
        max_results: int | None = None,
        time_range: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        topic: str = "general",
        deep: bool = False,
    ) -> str:
        if search is None:
            return "Веб-поиск не настроен: не задан TAVILY_API_KEY."

        cleaned = " ".join(str(query or "").split())
        if not cleaned:
            return "Поиск не выполнен: пустой запрос."
        if _only_site_operators(cleaned):
            return (
                "Поиск не выполнен: запрос состоит только из site:. Добавь "
                "ключевые слова, например: site:reddit.com Saltline отзывы."
            )

        default_results = safe_int(
            context.settings.get("search_max_results"),
            DEFAULT_RESULTS,
            minimum=1,
            maximum=MAX_RESULTS,
        )
        count = safe_int(
            max_results,
            default_results,
            minimum=1,
            maximum=MAX_RESULTS,
        )

        topic = str(topic or "general").strip().lower()
        if topic not in {"general", "news"}:
            return "Поиск не выполнен: topic должен быть general или news."

        period = str(time_range or "").strip().lower()
        if period and period not in TIME_RANGES:
            return (
                "Поиск не выполнен: time_range должен быть day, week, "
                "month или year."
            )
        try:
            start = _parse_date(start_date, "start_date")
            end = _parse_date(end_date, "end_date")
        except ValueError as exc:
            return f"Поиск не выполнен: {exc}."
        if period and (start or end):
            return (
                "Поиск не выполнен: нельзя одновременно указывать time_range "
                "и start_date/end_date."
            )
        if start and end and start > end:
            return "Поиск не выполнен: start_date позже end_date."

        if not (period or start or end):
            stale = _stale_date_error(cleaned)
            if stale:
                return stale

        kwargs: dict[str, Any] = {
            "query": cleaned,
            "max_results": count,
            "search_depth": "advanced" if deep else "basic",
            "topic": topic,
            "include_published_date": True,
            "chunks_per_source": 2,
        }
        if period:
            kwargs["time_range"] = period
        if start:
            kwargs["start_date"] = start
        if end:
            kwargs["end_date"] = end
        if period or start or end:
            kwargs["filter_by_published_date"] = True

        try:
            response = await search(**kwargs)
        except Exception as exc:  # noqa: BLE001 - Tavily raises its own errors
            detail = " ".join(str(exc).split())
            logger.warning("Веб-поиск не выполнен: %s", detail)
            return (
                f"Поиск не выполнен: {detail}"
                if detail
                else "Поиск не выполнен: сервис вернул ошибку."
            )
        results = response.get("results")
        if not isinstance(results, list) or not results:
            return "Поиск не дал результатов."

        blocks: list[str] = []
        for index, item in enumerate(results[:count], start=1):
            if not isinstance(item, dict):
                continue
            url = str(item.get("url") or "").strip()
            if url.startswith(("http://", "https://")):
                context.citations.append(
                    Citation(
                        title=str(item.get("title") or "").strip() or url,
                        url=url,
                    )
                )
            block = _format_result(index, item)
            if block:
                blocks.append(block)

        if not blocks:
            return "Поиск не дал результатов."
        return "\n---\n".join(blocks)

    return Tool(
        name="web_search",
        description=DESCRIPTION,
        parameters={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "2–6 ключевых слов с сущностями и датами; без вводных "
                        "слов. Точные названия и цитаты — в кавычках. Для "
                        "фактчека сформулируй проверяемое утверждение."
                    ),
                },
                "max_results": {
                    "type": "integer",
                    "description": (
                        "Сколько источников вернуть: 3 для быстрого факта, "
                        "5–8 для обзора или фактчека (1–10)."
                    ),
                    "minimum": 1,
                    "maximum": MAX_RESULTS,
                    "default": DEFAULT_RESULTS,
                },
                "time_range": {
                    "type": "string",
                    "description": (
                        "Свежесть результатов, если пользователь назвал период: "
                        "day, week, month или year."
                    ),
                    "enum": ["day", "week", "month", "year"],
                },
                "start_date": {
                    "type": "string",
                    "description": (
                        "Начало периода YYYY-MM-DD, если пользователь назвал "
                        "конкретную дату. Не вместе с time_range."
                    ),
                },
                "end_date": {
                    "type": "string",
                    "description": (
                        "Конец периода YYYY-MM-DD. Не вместе с time_range."
                    ),
                },
                "topic": {
                    "type": "string",
                    "description": (
                        "news для свежих новостей и событий, general для "
                        "остального."
                    ),
                    "enum": ["general", "news"],
                    "default": "general",
                },
                "deep": {
                    "type": "boolean",
                    "description": (
                        "true только если пользователь явно просит глубокое "
                        "исследование: advanced-поиск, 2 кредита."
                    ),
                    "default": False,
                },
            },
            "required": ["query"],
        },
        handler=web_search,
    )


def create_tool() -> Tool:
    api_key = os.getenv("TAVILY_API_KEY")
    if not api_key:
        return build_tool(None)

    try:
        from tavily import AsyncTavilyClient

        client = AsyncTavilyClient(api_key=api_key)
    except Exception:  # noqa: BLE001 - the bot must start without Tavily
        logger.exception("Не удалось инициализировать Tavily")
        return build_tool(None)

    return build_tool(client.search)
