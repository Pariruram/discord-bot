"""Message pipeline: addressing, memory, model call, tools and rendering."""

from __future__ import annotations

import asyncio
import contextlib
import io
import logging
import re
import uuid
from collections.abc import AsyncIterator
from typing import Any

import discord
from discord.ext import commands

from discord_bot.citations import Citation, format_sources
from discord_bot.discord_app.addressing import classify_message
from discord_bot.discord_app.images import attach_images, collect_image_parts
from discord_bot.discord_app.links import (
    collect_previews,
    format_link_block,
    urls_to_prefetch,
)
from discord_bot.discord_app.svg_input import collect_source_svg
from discord_bot.llm.client import LLMClient, LLMError
from discord_bot.llm.models import resolve_model_ref
from discord_bot.llm.providers import build_providers
from discord_bot.llm.tool_loop import run_tool_loop
from discord_bot.memory.service import MemoryService, conversation_scope
from discord_bot.memory.settings import MemorySettings
from discord_bot.storage.cache import SettingsCache
from discord_bot.tools.base import (
    GeneratedFile,
    ToolContext,
    ToolRegistry,
    enabled_tool_names,
)
from discord_bot.tools.fetch_url import ReadResult, read_url
from discord_bot.tools.svg import (
    is_drawing_request,
    is_svg_improvement_request,
    wants_pixel_art,
    wants_svg_source,
)
from discord_bot.utils import as_bool, safe_int, split_message

logger = logging.getLogger(__name__)

MAX_DISCORD_MESSAGE = 2000

# ``/c`` (or ``/clean``) at the very end asks for a factual, persona-free
# answer while keeping the whole conversational context. ``/svg`` at the end
# asks to attach the vector source of a drawing; ``/pixel`` asks for pixel art.
END_MARKER_RE = re.compile(
    r"(?:^|\s)/(clean|c|svg|pixel(?:\s+(?P<grid>\d{1,3}))?"
    r"(?::(?P<palette>[a-z0-9_-]+))?)\s*$",
    re.IGNORECASE,
)


def split_end_markers(text: str) -> tuple[str, bool, bool, bool, int, str]:
    """Return ``(text, clean, svg, pixel, grid, palette)``."""

    clean = False
    svg = False
    pixel = False
    grid = 0
    palette = ""
    value = text
    while True:
        match = END_MARKER_RE.search(value)
        if match is None:
            break
        stripped = value[: match.start()].rstrip()
        if not stripped:
            break
        marker = match.group(1).lower()
        if marker == "svg":
            svg = True
        elif marker.startswith("pixel"):
            pixel = True
            if match.group("grid"):
                grid = int(match.group("grid"))
            palette = (match.group("palette") or "").lower()
        else:
            clean = True
        value = stripped
    return value, clean, svg, pixel, grid, palette


def split_clean_marker(text: str) -> tuple[str, bool]:
    """Backward-compatible wrapper returning only the clean flag."""

    value, clean, _, _, _, _ = split_end_markers(text)
    return value, clean


@contextlib.asynccontextmanager
async def _typing(channel: Any) -> AsyncIterator[None]:
    """Show the typing indicator without failing the reply on a Discord 429."""

    factory = getattr(channel, "typing", None)
    if not callable(factory):
        yield
        return

    manager = factory()
    try:
        await manager.__aenter__()
    except discord.HTTPException as exc:
        logger.warning("Индикатор набора пропущен: %s", exc)
        yield
        return

    try:
        yield
    finally:
        with contextlib.suppress(discord.HTTPException):
            await manager.__aexit__(None, None, None)


class ChatService:
    def __init__(
        self,
        bot: commands.Bot,
        settings: SettingsCache,
        memory: MemoryService,
        llm: LLMClient,
        registry: ToolRegistry,
    ) -> None:
        self.bot = bot
        self.settings = settings
        self.memory = memory
        self.llm = llm
        self.registry = registry
        self._locks: dict[str, asyncio.Lock] = {}

    async def handle(self, message: discord.Message) -> None:
        try:
            await self._handle(message)
        except discord.HTTPException as exc:
            # Sending another message while Discord is rate-limiting us only
            # makes the block worse.
            logger.warning(
                "Discord API ошибка для сообщения %s: %s",
                getattr(message, "id", "?"),
                exc,
            )
        except Exception:  # noqa: BLE001 - top-level safety net for one message
            error_id = uuid.uuid4().hex[:8]
            logger.exception("Ошибка обработки сообщения (id=%s)", error_id)
            await self._reply_safe(
                message,
                f"Произошла ошибка, попробуй ещё раз. (код: {error_id})",
            )

    async def _handle(self, message: discord.Message) -> None:
        # Serialise the whole turn per conversation. The lock is taken before
        # the first await, so messages are answered in arrival order and the
        # next turn definitely sees the previous bot reply in the history.
        scope_type, scope_id, _ = conversation_scope(message)
        lock = self._lock_for(f"{scope_type}:{scope_id}")
        async with lock:
            await self._handle_locked(message)

    async def _handle_locked(self, message: discord.Message) -> None:
        settings = await self.settings.get_all()
        cfg = MemorySettings.from_mapping(settings)

        channel = message.channel
        if not isinstance(channel, discord.DMChannel):
            parent_id = getattr(channel, "parent_id", None)
            if not cfg.channel_allowed(
                str(channel.id),
                str(parent_id) if parent_id is not None else None,
            ):
                return

        incoming = classify_message(message, self.bot.user)
        if incoming.addressed_to != "bot" and not cfg.allowed_channel_ids:
            # With no explicit allowlist the bot only remembers conversations
            # that actually involve it, never ambient chatter in every channel.
            return

        (
            user_text,
            clean_mode,
            svg_source,
            pixel_art,
            pixel_grid,
            palette_name,
        ) = split_end_markers(message.content or "")
        image_parts, image_note = await collect_image_parts(message)
        source_svg = await collect_source_svg(message)
        stored_text = "\n".join(
            part
            for part in (user_text, image_note, source_svg.note)
            if part
        )
        result = await self.memory.ingest(
            message, incoming, content=stored_text or None
        )

        if result is not None and result.duplicate:
            # Discord replayed a message after a reconnect; it was already
            # stored and answered, so answering again would duplicate the reply.
            return

        if incoming.addressed_to != "bot":
            return

        providers = build_providers(settings)
        if not providers:
            await self._reply_safe(
                message,
                "Не настроен ни один LLM-провайдер. Добавь API-ключ и выбери модель через `!models`.",
            )
            return

        try:
            model_ref = resolve_model_ref(settings, providers)
        except ValueError:
            await self._reply_safe(message, "Не удалось определить модель. Проверь `!models`.")
            return
        spec = providers[model_ref.provider]

        prefetch_urls = urls_to_prefetch(message, settings)
        previews = (
            collect_previews(message, limit=len(prefetch_urls))
            if prefetch_urls
            else []
        )
        clean = self._clean_content(message, text=stored_text)

        async with _typing(message.channel):
            link_results, link_failures = await self._prefetch_links(prefetch_urls)
            stats: dict[str, Any] = {}
            system_prompt, chat_messages = await self.memory.build_context(
                message,
                incoming,
                result,
                settings,
                clean,
                stats=stats,
                clean_mode=clean_mode,
            )
            if link_results or previews:
                system_prompt = (
                    f"{system_prompt}\n\n"
                    f"{format_link_block(link_results, previews)}"
                )
            messages: list[dict[str, Any]] = [
                {"role": "system", "content": system_prompt},
                *chat_messages,
            ]
            attach_images(messages, image_parts)

            tool_context = ToolContext(
                user_id=str(message.author.id),
                channel_id=str(channel.id),
                guild_id=str(message.guild.id) if message.guild else None,
                settings=settings,
                llm=self.llm,
                provider=spec,
                model=model_ref.model,
                force_svg_source=svg_source,
                source_svg=source_svg.content,
                source_svg_name=source_svg.filename,
                force_pixel_art=pixel_art,
                force_pixel_grid=pixel_grid,
                pixel_palette=palette_name,
            )
            for link in link_results:
                tool_context.citations.append(
                    Citation(title=link.title, url=link.url)
                )
                tool_context.tool_trace.append(
                    {
                        "tool": "fetch_url",
                        "args": {"url": link.url},
                        "chars": len(link.text),
                        "auto": True,
                    }
                )
            for url, reason in link_failures:
                tool_context.tool_trace.append(
                    {
                        "tool": "fetch_url",
                        "args": {"url": url},
                        "error": reason[:200],
                        "auto": True,
                    }
                )
            enabled = enabled_tool_names(
                settings.get("enabled_tools"), self.registry
            )
            has_source_svg = bool(source_svg.content)
            pixel_requested = bool(
                pixel_art or wants_pixel_art(user_text)
            )
            if "draw_svg" in enabled:
                rule = (
                    "Никогда не выводи SVG/HTML-код для рисунка "
                    "в чат. Для любых рисунков используй инструмент draw_svg. "
                    "Если он вернул ошибку, коротко объясни её."
                )
                if has_source_svg:
                    rule += (
                        " Пользователь приложил SVG-исходник: если он просит "
                        "улучшить/перерисовать/исправить, вызови draw_svg — "
                        "исходник уже передан инструменту, не вставляй его в чат."
                    )
                if pixel_requested:
                    rule += " Если просят пиксель-арт, установи pixel_art=true."
                messages[0]["content"] = (
                    f"{messages[0]['content']}\n\n"
                    f"<drawing_rule>{rule}</drawing_rule>"
                )
            forced_tool = None
            if "draw_svg" in enabled:
                if (
                    is_svg_improvement_request(user_text, has_source_svg)
                    or is_drawing_request(user_text)
                    or pixel_art
                ):
                    forced_tool = "draw_svg"
            if forced_tool == "draw_svg":
                attach = (
                    "true"
                    if has_source_svg
                    or svg_source
                    or wants_svg_source(user_text)
                    else "false"
                )
                pixel = "true" if pixel_requested else "false"
                grid_hint = (
                    f" pixel_grid={pixel_grid}." if pixel_grid else ""
                )
                palette_hint = (
                    f" palette={palette_name}." if palette_name else ""
                )
                messages[0]["content"] = (
                    f"{messages[0]['content']}\n\n"
                    "<drawing_request>Пользователь явно просит рисунок. "
                    "Вызови draw_svg; не выводи SVG-код в чат. "
                    f"attach_svg={attach}. pixel_art={pixel}."
                    f"{grid_hint}{palette_hint} "
                    "Если инструмент вернул ошибку, объясни её коротко."
                    "</drawing_request>"
                )
            max_tokens = safe_int(
                settings.get("max_tokens"), 1200, minimum=200, maximum=4000
            )

            try:
                reply = await run_tool_loop(
                    self.llm,
                    spec,
                    model_ref,
                    messages,
                    self.registry,
                    enabled,
                    tool_context,
                    max_tokens=max_tokens,
                    forced_tool=forced_tool,
                )
            except LLMError as exc:
                logger.warning("LLM недоступна: %s", exc)
                await self._reply_safe(message, exc.user_message())
                return

            sources = format_sources(reply, tool_context.citations)
            sent = f"{reply}{sources}" if sources else reply

            metadata = {
                "provider": spec.name,
                "model": model_ref.model,
                "tool_calls": tool_context.tool_trace,
                "citations": [
                    {"title": citation.title, "url": citation.url}
                    for citation in tool_context.citations
                ],
                "files": [
                    {
                        "name": generated.filename,
                        "bytes": len(generated.data),
                        "mime": generated.mime,
                    }
                    for generated in tool_context.files
                ],
            }
            await self.memory.record_reply(message, result, reply, metadata)
            debug_mode = as_bool(settings.get("debug_mode"), False)

        max_len = safe_int(
            settings.get("max_message_length"),
            1900,
            minimum=200,
            maximum=MAX_DISCORD_MESSAGE,
        )
        await self._send(message, sent, max_len, files=tool_context.files)
        if debug_mode:
            await self._send_debug(message, spec, model_ref, tool_context, stats)

    async def _prefetch_links(
        self, urls: list[str]
    ) -> tuple[list[ReadResult], list[tuple[str, str]]]:
        """Fetch declared links in parallel; a failure never blocks the reply."""

        if not urls:
            return [], []
        outcomes = await asyncio.gather(
            *(read_url(url) for url in urls), return_exceptions=True
        )
        results: list[ReadResult] = []
        failures: list[tuple[str, str]] = []
        for url, outcome in zip(urls, outcomes, strict=True):
            if isinstance(outcome, ReadResult):
                results.append(outcome)
            else:
                reason = str(outcome) or type(outcome).__name__
                logger.warning("Ссылка не прочитана заранее: %s (%s)", url, reason)
                failures.append((url, reason))
        return results, failures

    async def _send_debug(
        self,
        message: discord.Message,
        spec: Any,
        model_ref: Any,
        tool_context: ToolContext,
        stats: dict[str, Any],
    ) -> None:
        """One factual line about this turn, no persona involved."""

        tool_parts: list[str] = []
        for entry in tool_context.tool_trace:
            args = entry.get("args") or {}
            args_text = ", ".join(
                f"{key}={str(value)[:60]}" for key, value in args.items()
            )
            mark = " ⨯" if entry.get("error") else ""
            if entry.get("auto"):
                mark += " [авто]"
            tool_parts.append(f"{entry.get('tool')}({args_text}){mark}")
        tools_text = " · ".join(tool_parts) if tool_parts else "нет"

        history_chars = int(stats.get("context_chars") or 0)
        files_text = (
            f" | файлы: {len(tool_context.files)}" if tool_context.files else ""
        )
        footer = (
            f"-# 🛠 {getattr(spec, 'name', '?')}/{getattr(model_ref, 'model', '?')}"
            f" | инструменты: {tools_text}"
            f" | контекст: {stats.get('history_messages', '?')} сообщ.,"
            f" {history_chars} симв.,"
            f" фактов: {stats.get('facts', '?')},"
            f" резюме: {'да' if stats.get('summary') else 'нет'}"
            f"{files_text}"
        )
        try:
            await message.channel.send(footer, suppress_embeds=True)
        except discord.HTTPException as exc:
            if getattr(exc, "status", None) == 429:
                logger.warning("Discord 429 при отправке debug-строки: %s", exc)
            else:
                logger.exception("Не удалось отправить debug-строку")

    def _lock_for(self, key: str) -> asyncio.Lock:
        if len(self._locks) > 1000:
            for old_key, lock in list(self._locks.items()):
                if old_key != key and not lock.locked():
                    self._locks.pop(old_key, None)
        return self._locks.setdefault(key, asyncio.Lock())

    def _clean_content(self, message: discord.Message, *, text: str | None = None) -> str:
        content = message.content if text is None else text
        if self.bot.user is not None:
            content = content.replace(f"<@{self.bot.user.id}>", "")
            content = content.replace(f"<@!{self.bot.user.id}>", "")
        return content.strip() or "Привет"

    def _discord_files(self, files: list[GeneratedFile]) -> list[discord.File]:
        return [
            discord.File(io.BytesIO(generated.data), filename=generated.filename)
            for generated in files
        ]

    async def _send(
        self,
        message: discord.Message,
        reply: str,
        max_len: int,
        *,
        files: list[GeneratedFile] | None = None,
    ) -> None:
        chunks = split_message(reply, max_len=max_len)
        if not chunks:
            chunks = ["Извини, не удалось сформировать ответ."]
        attachments = self._discord_files(files or [])
        try:
            try:
                await message.reply(
                    chunks[0], files=attachments, suppress_embeds=True
                )
            except discord.HTTPException as exc:
                if not attachments or getattr(exc, "status", None) == 429:
                    raise
                # A rejected attachment must not cost the user the whole reply.
                logger.warning(
                    "Не удалось отправить файлы (%s), повторяю без вложений",
                    exc,
                )
                await message.reply(chunks[0], suppress_embeds=True)
            for chunk in chunks[1:]:
                await message.channel.send(chunk, suppress_embeds=True)
        except discord.HTTPException as exc:
            if getattr(exc, "status", None) == 429:
                logger.warning(
                    "Discord 429 при отправке в канал %s: %s",
                    getattr(message.channel, "id", "?"),
                    exc,
                )
            else:
                logger.exception(
                    "Не удалось отправить ответ в канал %s",
                    getattr(message.channel, "id", "?"),
                )

    async def _reply_safe(self, message: discord.Message, text: str) -> None:
        try:
            await message.reply(text)
        except discord.HTTPException as exc:
            if getattr(exc, "status", None) == 429:
                logger.warning("Discord 429 при отправке ошибки: %s", exc)
            else:
                logger.exception("Не удалось отправить сообщение об ошибке")
