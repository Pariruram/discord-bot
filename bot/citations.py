"""Source citations for web-search-backed answers."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse

MAX_SOURCES = 5


@dataclass(frozen=True, slots=True)
class Citation:
    title: str
    url: str


def _is_http_url(url: str) -> bool:
    if not url or " " in url:
        return False
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _link_text(title: str, url: str) -> str:
    text = " ".join((title or "").split()) or url
    # Discord masked links break on square brackets in the label.
    if "[" in text or "]" in text:
        return url
    if len(text) > 120:
        return text[:119].rstrip() + "…"
    return text


def _needs_bare_link(url: str) -> bool:
    # Parentheses and spaces break Markdown link targets in Discord.
    return any(char in url for char in "()<> ")


def format_sources(
    reply: str,
    citations: list[Citation],
    *,
    limit: int = MAX_SOURCES,
    header: str = "**Источники:**",
) -> str:
    """Build a Discord-friendly sources footer.

    Only unique valid URLs that the model did not already mention are included.
    Returns an empty string when there is nothing to append.
    """

    if not citations or limit <= 0:
        return ""

    seen: set[str] = set()
    lines: list[str] = []

    for citation in citations:
        url = (citation.url or "").strip()
        if not _is_http_url(url) or url in seen:
            continue
        seen.add(url)

        if url in reply:
            continue

        label = _link_text(citation.title, url)
        if _needs_bare_link(url):
            lines.append(f"- {label}: {url}")
        else:
            lines.append(f"- [{label}]({url})")
        if len(lines) >= limit:
            break

    if not lines:
        return ""
    return f"\n\n{header}\n" + "\n".join(lines)
