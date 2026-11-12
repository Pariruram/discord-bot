"""Reddit thread tool: full RSS comments and an adaptive summary."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from discord_bot.citations import Citation
from discord_bot.discord_app.reddit import (
    find_reddit_url,
    parse_thread,
    thread_rss_url,
)
from discord_bot.llm.errors import LLMError
from discord_bot.tools.base import Tool, ToolContext
from discord_bot.tools.fetch_url import _http_fetch

logger = logging.getLogger(__name__)

MAX_SUMMARY_CHARS = 12_000
CHUNK_CHARS = 24_000
MAX_CHUNKS = 3
CHUNK_MAX_TOKENS = 1200
FINAL_MAX_TOKENS = 2000
REDDIT_CONTENT_TYPES = {
    "application/atom+xml",
    "application/rss+xml",
    "application/xml",
    "text/xml",
    "text/html",
}

DESCRIPTION = (
    "Читает Reddit-тред целиком через RSS и делает обобщение обсуждения. "
    "Используй для ссылок reddit.com/r/.../comments/... когда нужен пересказ "
    "комментариев, а не только заголовок. Для обычных статей используй "
    "fetch_url, для веб-поиска — web_search."
)

SUMMARY_PROMPT = (
    "Ты аналитик обсуждений. Сделай обобщение по существу, а не по объёму.\n"
    "Сначала реши, сколько нужно: простой тред — 2–4 предложения; сложный — "
    "короткие разделы или пункты.\n"
    "Не расписывай на 2000 слов то, что можно сказать предложением, и не "
    "ужимай в одно предложение то, что требует рассказа.\n"
    "Обязательно отрази: главный тезис поста, общий консенсус, основные "
    "разногласия и аргументы сторон, 2–5 характерных цитат или мыслей.\n"
    "Не пересказывай каждый комментарий, не выдумывай и не добавляй оценок. "
    "Верни только текст обобщения."
)

CHUNK_PROMPT = (
    "Ниже часть Reddit-обсуждения. Сделай сжатое резюме именно этой части: "
    "сохрани аргументы, возражения и характерные цитаты. Не делай финальных "
    "выводов по всему треду."
)


async def _complete(
    context: ToolContext,
    system: str,
    user: str,
    *,
    max_tokens: int,
    purpose: str = "read_thread",
) -> str:
    if context.llm is None or context.provider is None:
        raise RuntimeError("нет LLM-контекста")
    model = context.model or str(
        getattr(context.provider, "default_model", "") or ""
    )
    return await context.llm.complete_text(
        context.provider,
        model,
        [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        temperature=0.3,
        max_tokens=max_tokens,
        max_attempts=1,
        purpose=purpose,
    )


def _chunks(text: str) -> list[str]:
    if len(text) <= CHUNK_CHARS:
        return [text]
    chunks: list[str] = []
    remaining = text
    for _ in range(MAX_CHUNKS):
        if len(remaining) <= CHUNK_CHARS:
            chunks.append(remaining)
            remaining = ""
            break
        cut = remaining.rfind("\n", 0, CHUNK_CHARS)
        if cut < CHUNK_CHARS // 2:
            cut = CHUNK_CHARS
        chunks.append(remaining[:cut])
        remaining = remaining[cut:].lstrip()
    if remaining:
        chunks[-1] += "\n" + remaining
    return chunks


async def summarize_thread(text: str, context: ToolContext) -> str:
    """Summarize the whole thread, adapting the length to its content."""

    chunks = _chunks(text)
    logger.info(
        "Reddit summary: text_chars=%s chunks=%s", len(text), len(chunks)
    )
    if len(chunks) == 1:
        summary = await _complete(
            context,
            SUMMARY_PROMPT,
            text,
            max_tokens=FINAL_MAX_TOKENS,
            purpose="read_thread_final",
        )
        return summary.strip()[:MAX_SUMMARY_CHARS]

    partials: list[str] = []
    for index, chunk in enumerate(chunks, start=1):
        logger.info(
            "Reddit chunk %s/%s chars=%s", index, len(chunks), len(chunk)
        )
        partials.append(
            await _complete(
                context,
                CHUNK_PROMPT,
                chunk,
                max_tokens=CHUNK_MAX_TOKENS,
                purpose=f"read_thread_chunk_{index}/{len(chunks)}",
            )
        )
    combined = "\n\n".join(
        f"Часть {index}:\n{part.strip()}"
        for index, part in enumerate(partials, start=1)
    )
    summary = await _complete(
        context,
        SUMMARY_PROMPT,
        combined,
        max_tokens=FINAL_MAX_TOKENS,
        purpose="read_thread_final",
    )
    return summary.strip()[:MAX_SUMMARY_CHARS]


Fetcher = Callable[..., Awaitable[Any]]


def build_tool(fetcher: Fetcher | None = None) -> Tool:
    fetch = fetcher or _http_fetch

    async def read_thread(context: ToolContext, url: str) -> str:
        target = find_reddit_url(url)
        if target is None:
            return "Это не похоже на ссылку Reddit-треда."
        rss_url = thread_rss_url(target)
        if rss_url is None:
            return "Не удалось разобрать ссылку Reddit."

        try:
            page = await fetch(
                rss_url, allowed_types=REDDIT_CONTENT_TYPES
            )
            thread = parse_thread(page.text, target)
        except LLMError:
            raise
        except Exception as exc:  # noqa: BLE001 - user-facing RSS failures
            logger.warning("Reddit RSS не прочитан: %s", exc)
            return (
                "Не удалось прочитать Reddit-тред: RSS недоступен или "
                "Reddit заблокировал запрос."
            )

        if not thread.comments:
            return "В этом Reddit-треде нет доступных комментариев."

        thread_text = thread.as_text()
        logger.info(
            "Reddit thread comments=%s text_chars=%s",
            len(thread.comments),
            len(thread_text),
        )
        try:
            summary = await summarize_thread(thread_text, context)
        except LLMError as exc:
            logger.warning("Reddit-обобщение не выполнено: %s", exc)
            return (
                "Reddit-тред прочитан, но модель обобщения перегружена. "
                "Попробуй ещё раз позже."
            )
        except Exception:  # noqa: BLE001
            logger.exception("Ошибка обобщения Reddit")
            return "Не удалось обобщить Reddit-тред."

        if not summary.strip():
            return "Модель не вернула обобщение Reddit-треда."

        context.citations.append(
            Citation(
                title=thread.title or "Reddit thread",
                url=target,
            )
        )
        return (
            f"Reddit-тред: {thread.title}\n"
            f"URL: {target}\n"
            f"Комментариев: {len(thread.comments)}\n\n"
            f"{summary}"
        )

    return Tool(
        name="read_thread",
        description=DESCRIPTION,
        parameters={
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "Ссылка на Reddit-тред с комментариями.",
                }
            },
            "required": ["url"],
        },
        handler=read_thread,
        timeout=120.0,
        max_result_chars=MAX_SUMMARY_CHARS,
    )


def create_tool() -> Tool:
    return build_tool()
