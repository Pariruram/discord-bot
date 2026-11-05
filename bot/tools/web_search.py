"""Tavily web search tool with source citations."""

from __future__ import annotations

import logging
import os
from collections.abc import Awaitable, Callable
from typing import Any

from bot.citations import Citation
from bot.tools.base import Tool, ToolContext

logger = logging.getLogger(__name__)

SearchFn = Callable[..., Awaitable[dict[str, Any]]]

DESCRIPTION = (
    "Используй ТОЛЬКО когда пользователь явно просит найти свежие данные, "
    "конкретные новости, текущие даты, актуальные курсы или фактчек событий "
    "после 2024 года. НЕ используй для общих вопросов, программирования, "
    "размышлений или бытовых тем. После использования обязательно укажи "
    "ссылки на источники в ответе."
)


def _format_result(index: int, item: dict[str, Any]) -> str:
    title = str(item.get("title") or "").strip()
    url = str(item.get("url") or "").strip()
    content = str(item.get("content") or "").strip()

    parts: list[str] = []
    if title:
        parts.append(f"[{index}] {title}")
    if url:
        parts.append(url)
    if content:
        parts.append(content)
    return "\n".join(parts)


def build_tool(search: SearchFn | None) -> Tool:
    """Build the tool with an injected search callable (for tests and DI)."""

    async def web_search(context: ToolContext, query: str) -> str:
        if search is None:
            return "Веб-поиск не настроен: не задан TAVILY_API_KEY."

        response = await search(
            query=query,
            max_results=3,
            search_depth="basic",
        )
        results = response.get("results")
        if not isinstance(results, list) or not results:
            return "Поиск не дал результатов."

        blocks: list[str] = []
        for index, item in enumerate(results[:3], start=1):
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
                        "Точный, лаконичный поисковый запрос (2-4 ключевых слова без "
                        "вводных слов). Пример: 'Elon Musk news November 2026'."
                    ),
                }
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
