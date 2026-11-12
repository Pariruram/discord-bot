"""Read a URL as text: articles, docs and YouTube pages.

The tool only fetches the specific link the user gave. It refuses non-HTTP
URLs, credentials in the URL, non-standard ports and private/loopback hosts
(including redirect targets), and caps the response body as well as the text
returned to the model.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
import socket
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

import httpx

from discord_bot.citations import Citation
from discord_bot.tools.base import Tool, ToolContext
from discord_bot.tools.youtube import (
    VideoInfo,
    extract_player_response,
    fetch_transcript_fallback,
    format_duration,
    parse_transcript,
    parse_video_id,
    pick_caption_track,
    transcript_url,
    video_info,
)

logger = logging.getLogger(__name__)

MAX_BYTES = 3 * 1024 * 1024
MAX_RETURN_CHARS = 5600
MAX_REDIRECTS = 3
REQUEST_BUDGET_SECONDS = 12.0
YOUTUBE_PAGE_BUDGET_SECONDS = 8.0
YOUTUBE_TRANSCRIPT_BUDGET_SECONDS = 5.0

ALLOWED_CONTENT_TYPES = {
    "text/html",
    "application/xhtml+xml",
    "text/plain",
    "application/json",
}
SKIP_TAGS = {"script", "style", "noscript", "svg", "template", "iframe"}
BLOCK_TAGS = {
    "p",
    "div",
    "br",
    "li",
    "tr",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "section",
    "article",
    "header",
    "footer",
    "nav",
    "blockquote",
    "pre",
    "table",
}
YOUTUBE_COOKIES = "CONSENT=YES+cb; SOCS=CAI"
USER_AGENT = (
    "Mozilla/5.0 (compatible; slop-discord-bot/1.0; "
    "+https://github.com/Pariruram/discord-bot)"
)
DESCRIPTION = (
    "Читает содержимое конкретной ссылки, которую прислал пользователь: "
    "статью, документацию, YouTube-видео. Для YouTube возвращает название, "
    "описание и субтитры. Используй, когда нужно ответить по сути страницы. "
    "НЕ используй вместо поиска — для поиска есть web_search. Не вызывай для "
    "ссылок из результатов поиска, если не просили прочитать их полностью."
)


class FetchError(Exception):
    """A user-facing fetch failure."""


@dataclass(frozen=True, slots=True)
class FetchedPage:
    url: str
    status: int
    content_type: str
    text: str
    truncated: bool = False


@dataclass(frozen=True, slots=True)
class ReadResult:
    text: str
    title: str
    url: str


Fetcher = Callable[..., Awaitable[FetchedPage]]
Resolver = Callable[[str, int], list[str]]


# --- URL safety ------------------------------------------------------------


def _resolve_default(host: str, port: int) -> list[str]:
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return sorted({str(info[4][0]) for info in infos})


def _is_public_address(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


def normalize_url(url: str) -> str:
    """Add ``https://`` to a bare host, as users often type ``youtu.be/...``."""

    text = (url or "").strip()
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", text):
        text = f"https://{text.lstrip('/')}"
    return text


async def validate_url(
    url: str, resolver: Resolver | None = None
) -> tuple[bool, str]:
    """Return ``(ok, reason)``; rejects hosts that could hit the internal net."""

    try:
        parts = urlsplit(url)
    except ValueError:
        return False, "некорректная ссылка"
    if parts.scheme not in {"http", "https"}:
        return False, "поддерживаются только http и https"
    if parts.username or parts.password:
        return False, "ссылки с логином и паролем не поддерживаются"
    host = (parts.hostname or "").strip().lower()
    if not host:
        return False, "в ссылке нет домена"
    if host in {"localhost", "localhost.localdomain"} or host.endswith(
        (".local", ".internal", ".localhost", ".lan", ".home.arpa")
    ):
        return False, "локальные адреса запрещены"
    try:
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError:
        return False, "некорректный порт"
    if port not in {80, 443}:
        return False, "разрешены только порты 80 и 443"

    resolve = resolver or _resolve_default
    try:
        addresses = await asyncio.to_thread(resolve, host, port)
    except OSError:
        return False, "не удалось разрешить домен"
    if not addresses:
        return False, "не удалось разрешить домен"
    if any(not _is_public_address(address) for address in addresses):
        return False, "адрес ведёт во внутреннюю сеть"
    return True, ""


# --- HTTP ------------------------------------------------------------------


def _decode(data: bytes, content_type: str) -> str:
    charset = ""
    match = re.search(r"charset=([\w-]+)", content_type, re.IGNORECASE)
    if match:
        charset = match.group(1)
    for encoding in (charset, "utf-8", "cp1251"):
        if not encoding:
            continue
        try:
            return data.decode(encoding, errors="replace")
        except LookupError:
            continue
    return data.decode("utf-8", errors="replace")


async def _http_fetch(
    url: str,
    *,
    resolver: Resolver | None = None,
    budget: float = REQUEST_BUDGET_SECONDS,
    transport: httpx.AsyncBaseTransport | None = None,
    allowed_types: set[str] | None = None,
) -> FetchedPage:
    """GET a page with manual redirects, each one re-validated."""

    async with asyncio.timeout(budget):
        return await _http_fetch_inner(
            url,
            resolver=resolver,
            budget=budget,
            transport=transport,
            allowed_types=allowed_types,
        )


async def _http_fetch_inner(
    url: str,
    *,
    resolver: Resolver | None = None,
    budget: float = REQUEST_BUDGET_SECONDS,
    transport: httpx.AsyncBaseTransport | None = None,
    allowed_types: set[str] | None = None,
) -> FetchedPage:
    deadline = time.monotonic() + budget
    current = url
    async with httpx.AsyncClient(
        follow_redirects=False,
        timeout=httpx.Timeout(8.0, connect=5.0),
        transport=transport,
        headers={
            "User-Agent": USER_AGENT,
            "Accept-Language": "ru,en;q=0.8",
        },
    ) as client:
        for _ in range(MAX_REDIRECTS + 1):
            ok, reason = await validate_url(current, resolver)
            if not ok:
                raise FetchError(reason)
            remaining = deadline - time.monotonic()
            if remaining <= 0.5:
                raise FetchError("страница отвечает слишком долго")

            host = (urlsplit(current).hostname or "").lower()
            headers = (
                {"Cookie": YOUTUBE_COOKIES} if host.endswith("youtube.com") else None
            )
            try:
                async with client.stream(
                    "GET", current, headers=headers
                ) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        location = response.headers.get("location")
                        if not location:
                            raise FetchError("редирект без адреса")
                        current = urljoin(current, location)
                        continue
                    if response.status_code >= 400:
                        raise FetchError(f"страница вернула HTTP {response.status_code}")

                    content_type = str(
                        response.headers.get("content-type") or ""
                    )
                    base_type = content_type.split(";")[0].strip().lower()
                    allowed = allowed_types or ALLOWED_CONTENT_TYPES
                    if base_type and base_type not in allowed:
                        raise FetchError("это не текстовая страница")

                    data = bytearray()
                    truncated = False
                    async for chunk in response.aiter_bytes():
                        data.extend(chunk)
                        if len(data) > MAX_BYTES:
                            del data[MAX_BYTES:]
                            truncated = True
                            break
                        if time.monotonic() > deadline:
                            truncated = True
                            break

                    return FetchedPage(
                        url=current,
                        status=response.status_code,
                        content_type=content_type,
                        text=_decode(bytes(data), content_type),
                        truncated=truncated,
                    )
            except httpx.HTTPError as exc:
                raise FetchError("сетевая ошибка или таймаут") from exc

        raise FetchError("слишком много редиректов")


# --- HTML -> text ----------------------------------------------------------


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self._in_title = False
        self.title_parts: list[str] = []
        self.description = ""
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in SKIP_TAGS:
            self._skip_depth += 1
            return
        if tag == "title":
            self._in_title = True
        if tag == "meta":
            self._read_meta(attrs)
        if tag in BLOCK_TAGS and not self._skip_depth:
            self.parts.append("\n")

    def handle_startendtag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        if tag == "meta":
            self._read_meta(attrs)
        if tag in BLOCK_TAGS and not self._skip_depth:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1
            return
        if tag == "title":
            self._in_title = False
        if tag in BLOCK_TAGS and not self._skip_depth:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title_parts.append(data)
        if self._skip_depth:
            return
        self.parts.append(data)

    def _read_meta(self, attrs: list[tuple[str, str | None]]) -> None:
        values = {key.lower(): (value or "") for key, value in attrs}
        name = values.get("name") or values.get("property")
        if name in {"description", "og:description"} and not self.description:
            self.description = values.get("content", "")


def _normalize(raw: str) -> str:
    raw = raw.replace("\xa0", " ")
    lines = [" ".join(line.split()) for line in raw.splitlines()]
    text = "\n".join(line for line in lines if line)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def extract_text(page: str) -> tuple[str, str, str]:
    """Return ``(title, meta_description, readable_text)``."""

    extractor = _TextExtractor()
    try:
        extractor.feed(page)
        extractor.close()
    except Exception:  # noqa: BLE001 - broken HTML should not fail the tool
        logger.debug("Не удалось разобрать HTML", exc_info=True)
    title = _normalize("".join(extractor.title_parts))
    description = _normalize(extractor.description)
    text = _normalize("".join(extractor.parts))
    return title, description, text


def _format_page(page: FetchedPage) -> tuple[str, str]:
    title, description, text = extract_text(page.text)
    body = text
    if len(body) < 200 and description:
        body = description + ("\n\n" + body if body else "")

    lines = [f"URL: {page.url}"]
    if title:
        lines.append(f"Заголовок: {title}")
    if page.truncated:
        lines.append("(страница обрезана по лимиту размера)")
    lines.append("---")
    body = body[:MAX_RETURN_CHARS].strip()
    lines.append(body or "(на странице нет читаемого текста)")
    return "\n".join(lines), title or page.url


def _watch_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


def _format_video(info: VideoInfo, url: str) -> str:
    lines = [f"URL: {url}", f"Название: {info.title}"]
    meta = []
    if info.author:
        meta.append(f"Автор: {info.author}")
    duration = format_duration(info.length_seconds)
    if duration:
        meta.append(f"Длительность: {duration}")
    if meta:
        lines.append(" | ".join(meta))

    if info.transcript:
        kind = "авто" if info.transcript_auto else "ручные"
        language = info.transcript_language or "?"
        lines.append(
            f"Субтитры: {language} ({kind}), {len(info.transcript)} симв."
        )
        body = info.transcript[:MAX_RETURN_CHARS]
    else:
        lines.append("Субтитры: недоступны — ниже только описание")
        body = info.description[:MAX_RETURN_CHARS]
    lines.append("---")
    lines.append(body.strip() or "(описание отсутствует)")
    return "\n".join(lines)


async def _youtube_result(
    video_id: str, fetcher: Fetcher
) -> tuple[str, str]:
    url = _watch_url(video_id)
    page = await fetcher(url, budget=YOUTUBE_PAGE_BUDGET_SECONDS)
    player = extract_player_response(page.text)
    if player is None:
        raise FetchError(
            "YouTube не отдал данные видео (бот-проверка или consent-страница)"
        )

    track = pick_caption_track(player)
    transcript = ""
    language = ""
    auto: bool | None = None
    if track is not None:
        track_url = transcript_url(track)
        if track_url:
            try:
                payload = await fetcher(
                    track_url, budget=YOUTUBE_TRANSCRIPT_BUDGET_SECONDS
                )
                transcript = parse_transcript(payload.text)
            except FetchError:
                transcript = ""
        if transcript:
            language = str(track.get("languageCode") or "")
            auto = str(track.get("kind") or "") == "asr"

    if not transcript:
        transcript, language, auto = await fetch_transcript_fallback(video_id)

    info = video_info(
        video_id, player, transcript, track, language=language, auto=auto
    )
    if not info.title:
        raise FetchError("не удалось получить название видео")
    return _format_video(info, url), info.title or url


# --- Tool ------------------------------------------------------------------


async def read_url(
    url: str,
    *,
    fetcher: Fetcher | None = None,
    resolver: Resolver | None = None,
) -> ReadResult:
    """Fetch one URL and return the text that should reach the model."""

    target = normalize_url(url)
    ok, reason = await validate_url(target, resolver)
    if not ok:
        raise FetchError(reason)

    fetch = fetcher or _http_fetch
    video_id = parse_video_id(target)
    if video_id is not None:
        text, title = await _youtube_result(video_id, fetch)
        return ReadResult(text, title, _watch_url(video_id))

    page = await fetch(target, budget=REQUEST_BUDGET_SECONDS)
    text, title = _format_page(page)
    return ReadResult(text, title, page.url)


def build_tool(
    fetcher: Fetcher | None = None, resolver: Resolver | None = None
) -> Tool:
    async def fetch_url(context: ToolContext, url: str) -> str:
        try:
            result = await read_url(url, fetcher=fetcher, resolver=resolver)
            context.citations.append(
                Citation(title=result.title, url=result.url)
            )
            return result.text
        except FetchError as exc:
            return f"Не удалось прочитать ссылку: {exc}."
        except TimeoutError:
            return "Не удалось прочитать ссылку: таймаут."
        except Exception:  # noqa: BLE001 - a tool failure must not break the chat
            logger.exception("Ошибка fetch_url для %s", url)
            return "Не удалось прочитать ссылку: внутренняя ошибка."

    return Tool(
        name="fetch_url",
        description=DESCRIPTION,
        parameters={
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": (
                        "Полная http(s)-ссылка, которую пользователь просит "
                        "прочитать. Пример: 'https://example.com/article'."
                    ),
                }
            },
            "required": ["url"],
        },
        handler=fetch_url,
    )


def create_tool() -> Tool:
    return build_tool()
