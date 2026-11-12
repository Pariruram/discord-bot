"""Reddit thread reading via the public Atom/RSS feed."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from defusedxml import ElementTree as ET

from discord_bot.tools.fetch_url import extract_text

REDDIT_THREAD_RE = re.compile(
    r"https?://(?:www\.|old\.)?reddit\.com/r/(?P<sub>[^/\s]+)"
    r"/comments/(?P<id>[a-z0-9]+)(?:/[^\s]*)?",
    re.IGNORECASE,
)
ATOM = "{http://www.w3.org/2005/Atom}"
MAX_COMMENTS = 500
MAX_TEXT_CHARS = 80_000


@dataclass(slots=True)
class RedditThread:
    url: str
    title: str = ""
    author: str = ""
    post_text: str = ""
    comments: list[tuple[str, str]] = field(default_factory=list)

    def as_text(self, limit: int = MAX_TEXT_CHARS) -> str:
        parts = [f"Пост: {self.title}"]
        if self.author:
            parts.append(f"Автор поста: {self.author}")
        if self.post_text:
            parts.append(self.post_text)
        parts.append(f"--- комментарии ({len(self.comments)}) ---")
        total = len("\n".join(parts))
        for index, (author, text) in enumerate(self.comments, start=1):
            block = f"[{index}] {author}: {text}"
            if total + len(block) + 1 > limit:
                parts.append(f"... ещё {len(self.comments) - index + 1} комментариев не поместились")
                break
            parts.append(block)
            total += len(block) + 1
        return "\n".join(parts)


def find_reddit_url(text: str | None) -> str | None:
    match = REDDIT_THREAD_RE.search(text or "")
    if match is None:
        return None
    return match.group(0).rstrip(".,;!?)")


def thread_rss_urls(url: str) -> list[str]:
    """RSS hosts for a thread: canonical Reddit plus its media CDN alias."""

    match = REDDIT_THREAD_RE.search(url or "")
    if match is None:
        return []
    path = (
        f"r/{match.group('sub')}/comments/{match.group('id')}/.rss"
    )
    return [
        f"https://www.reddit.com/{path}",
        f"https://www.redditmedia.com/{path}",
    ]


def thread_rss_url(url: str) -> str | None:
    urls = thread_rss_urls(url)
    return urls[0] if urls else None


def thread_label(url: str) -> str:
    """Short human-readable citation label for a Reddit thread URL."""

    match = REDDIT_THREAD_RE.search(url or "")
    if match is None:
        return "Reddit thread"
    slug = re.search(
        r"/comments/[a-z0-9]+/([^/?#\s]+)", url or "", re.IGNORECASE
    )
    if slug:
        words = slug.group(1).replace("_", " ").strip()
        if words:
            return f"r/{match.group('sub')}: {words}"
    return f"r/{match.group('sub')} thread"


def _entry_text(entry: Any) -> str:
    content = entry.find(f"{ATOM}content")
    if content is None:
        return ""
    raw = "".join(content.itertext()) or content.text or ""
    _, _, text = extract_text(raw)
    return text.strip()


def parse_thread(feed: str, url: str) -> RedditThread:
    """Parse a Reddit Atom feed into the post and its comments."""

    root = ET.fromstring(feed)
    entries = root.findall(f"{ATOM}entry")
    if not entries:
        raise ValueError("Reddit не вернул комментарии")

    first = entries[0]
    title = (root.findtext(f"{ATOM}title") or "").strip()
    if not title:
        title = (first.findtext(f"{ATOM}title") or "").strip()
    author_node = first.find(f"{ATOM}author")
    author = (
        (author_node.findtext(f"{ATOM}name") if author_node is not None else "")
        or ""
    ).strip()
    thread = RedditThread(
        url=url,
        title=title,
        author=author,
        post_text=_entry_text(first),
    )

    for entry in entries[1:]:
        if len(thread.comments) >= MAX_COMMENTS:
            break
        author_node = entry.find(f"{ATOM}author")
        name = (
            (author_node.findtext(f"{ATOM}name") if author_node is not None else "")
            or "unknown"
        ).strip()
        text = _entry_text(entry)
        if not text or text.lower() in {"[deleted]", "[removed]"}:
            continue
        thread.comments.append((name, text))
    return thread
