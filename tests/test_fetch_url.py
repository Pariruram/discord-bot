from __future__ import annotations

import json

import httpx
import pytest

from discord_bot.tools.base import ToolContext, enabled_tool_names
from discord_bot.tools.fetch_url import (
    FetchedPage,
    FetchError,
    _http_fetch,
    build_tool,
    extract_text,
    normalize_url,
    validate_url,
)


def public_resolver(host: str, port: int) -> list[str]:
    return ["93.184.216.34"]


class FakeFetcher:
    def __init__(self, page: str, transcript: str | None = None) -> None:
        self.page = page
        self.transcript = transcript or ""
        self.calls: list[str] = []

    async def __call__(self, url: str, *, budget: float = 0) -> FetchedPage:
        self.calls.append(url)
        if "timedtext" in url:
            return FetchedPage(
                url=url,
                status=200,
                content_type="application/json",
                text=self.transcript,
            )
        return FetchedPage(
            url=url,
            status=200,
            content_type="text/html; charset=utf-8",
            text=self.page,
        )


def context() -> ToolContext:
    return ToolContext(user_id="1", channel_id="2")


def test_normalize_url_adds_https_to_bare_hosts() -> None:
    assert normalize_url("youtu.be/dQw4w9WgXcQ") == "https://youtu.be/dQw4w9WgXcQ"
    assert normalize_url("https://example.com") == "https://example.com"


async def test_validate_url_rejects_dangerous_targets() -> None:
    for url in (
        "ftp://example.com/file",
        "https://user:pass@example.com/",
        "http://localhost/admin",
        "http://my-host.internal/",
        "https://example.com:8080/",
    ):
        ok, reason = await validate_url(url, public_resolver)
        assert ok is False, url
        assert reason

    ok, _ = await validate_url("https://example.com/article", public_resolver)
    assert ok is True


async def test_validate_url_rejects_private_addresses() -> None:
    for address in (
        "127.0.0.1",
        "10.0.0.5",
        "169.254.169.254",
        "192.168.1.1",
        "::1",
    ):
        ok, reason = await validate_url(
            "http://example.com/", lambda host, port, a=address: [a]
        )
        assert ok is False, address
        assert "внутреннюю" in reason


async def test_validate_url_handles_dns_failure() -> None:
    def broken(host: str, port: int) -> list[str]:
        raise OSError("no dns")

    ok, reason = await validate_url("https://example.com", broken)

    assert ok is False
    assert "домен" in reason


def test_extract_text_drops_scripts_and_keeps_structure() -> None:
    page = """
    <html><head>
      <title>Заголовок страницы</title>
      <meta name="description" content="Краткое описание">
      <style>.x { color: red }</style>
    </head><body>
      <script>alert('nope')</script>
      <h1>Большой заголовок</h1>
      <p>Первый   абзац.</p>
      <p>Второй&nbsp;абзац.</p>
    </body></html>
    """

    title, description, text = extract_text(page)

    assert title == "Заголовок страницы"
    assert description == "Краткое описание"
    assert "alert" not in text
    assert "color: red" not in text
    assert "Большой заголовок" in text
    assert "Первый абзац." in text
    assert "Второй абзац." in text


async def test_tool_returns_page_text_and_a_citation() -> None:
    fetcher = FakeFetcher(
        "<html><head><title>Статья</title></head>"
        "<body><p>Полезный текст статьи.</p></body></html>"
    )
    tool = build_tool(fetcher, resolver=public_resolver)
    ctx = context()

    result = await tool.handler(ctx, "https://example.com/article")

    assert "Заголовок: Статья" in result
    assert "Полезный текст статьи." in result
    assert ctx.citations[0].url == "https://example.com/article"
    assert ctx.citations[0].title == "Статья"


async def test_tool_falls_back_to_meta_description() -> None:
    fetcher = FakeFetcher(
        '<html><head><title>SPA</title>'
        '<meta name="description" content="Описание из мета-тега">'
        "</head><body><div id=app></div></body></html>"
    )
    tool = build_tool(fetcher, resolver=public_resolver)

    result = await tool.handler(context(), "https://example.com/app")

    assert "Описание из мета-тега" in result


async def test_tool_reports_a_fetch_failure() -> None:
    class Broken:
        async def __call__(self, url: str, *, budget: float = 0) -> FetchedPage:
            raise FetchError("это не текстовая страница")

    tool = build_tool(Broken(), resolver=public_resolver)

    result = await tool.handler(context(), "https://example.com/file.pdf")

    assert result == "Не удалось прочитать ссылку: это не текстовая страница."


async def test_private_host_is_rejected_before_any_fetch() -> None:
    fetcher = FakeFetcher("<html></html>")
    tool = build_tool(fetcher, resolver=lambda host, port: ["10.0.0.1"])

    result = await tool.handler(context(), "http://internal.example.com/")

    assert "внутреннюю сеть" in result
    assert fetcher.calls == []


async def test_redirect_to_a_private_host_is_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "http://127.0.0.1/admin"})

    def resolver(host: str, port: int) -> list[str]:
        return ["93.184.216.34"] if host == "example.com" else ["127.0.0.1"]

    with pytest.raises(FetchError):
        await _http_fetch(
            "https://example.com/start",
            resolver=resolver,
            transport=httpx.MockTransport(handler),
        )


async def test_binary_content_type_is_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "application/pdf"},
            content=b"%PDF-1.7",
        )

    with pytest.raises(FetchError):
        await _http_fetch(
            "https://example.com/file.pdf",
            resolver=public_resolver,
            transport=httpx.MockTransport(handler),
        )


async def test_fetch_url_is_enabled_by_default() -> None:
    from discord_bot.tools.base import Tool, ToolRegistry

    async def dummy(context: object) -> str:
        return ""

    registry = ToolRegistry(timeout=1.0)
    registry.register(build_tool(FakeFetcher("<html></html>"), public_resolver))
    registry.register(
        Tool(name="web_search", description="", parameters={}, handler=dummy)
    )

    names = enabled_tool_names(None, registry)

    assert "fetch_url" in names


# --- YouTube ---------------------------------------------------------------

VIDEO_ID = "dQw4w9WgXcQ"


def youtube_page(with_captions: bool = True) -> str:
    player = {
        "videoDetails": {
            "videoId": VIDEO_ID,
            "title": "Тестовое видео",
            "author": "Автор канала",
            "lengthSeconds": "754",
            "shortDescription": "Описание видео.",
        },
    }
    if with_captions:
        player["captions"] = {
            "playerCaptionsTracklistRenderer": {
                "captionTracks": [
                    {
                        "baseUrl": (
                            "https://www.youtube.com/api/timedtext?"
                            f"v={VIDEO_ID}&lang=ru"
                        ),
                        "languageCode": "ru",
                    }
                ]
            }
        }
    return (
        "<html><head><title>Тестовое видео - YouTube</title></head><body>"
        f"<script>var ytInitialPlayerResponse = {json.dumps(player)};</script>"
        "</body></html>"
    )


async def test_youtube_link_returns_metadata_and_transcript() -> None:
    fetcher = FakeFetcher(
        youtube_page(),
        json.dumps({"events": [{"segs": [{"utf8": "Привет из видео"}]}]}),
    )
    tool = build_tool(fetcher, resolver=public_resolver)
    ctx = context()

    result = await tool.handler(ctx, f"https://youtu.be/{VIDEO_ID}")

    assert "Название: Тестовое видео" in result
    assert "Автор: Автор канала" in result
    assert "Длительность: 12:34" in result
    assert "Субтитры: ru (ручные)" in result
    assert "Привет из видео" in result
    assert ctx.citations[0].url == f"https://www.youtube.com/watch?v={VIDEO_ID}"
    assert any("timedtext" in call for call in fetcher.calls)


async def test_youtube_without_captions_falls_back_to_description() -> None:
    fetcher = FakeFetcher(youtube_page(with_captions=False))
    tool = build_tool(fetcher, resolver=public_resolver)

    result = await tool.handler(
        context(), f"https://www.youtube.com/watch?v={VIDEO_ID}"
    )

    assert "Субтитры: недоступны" in result
    assert "Описание видео." in result


async def test_youtube_consent_page_is_reported() -> None:
    fetcher = FakeFetcher("<html><body>Before you continue</body></html>")
    tool = build_tool(fetcher, resolver=public_resolver)

    result = await tool.handler(context(), f"https://youtu.be/{VIDEO_ID}")

    assert "Не удалось прочитать ссылку" in result
    assert "consent" in result
