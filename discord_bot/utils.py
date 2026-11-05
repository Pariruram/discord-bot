"""Small shared helpers: parsing, truncation and message splitting."""

from __future__ import annotations

import json
from typing import Any, TypeVar

T = TypeVar("T")


def safe_int(value: Any, default: int, *, minimum: int | None = None, maximum: int | None = None) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError):
        result = default
    if minimum is not None:
        result = max(minimum, result)
    if maximum is not None:
        result = min(maximum, result)
    return result


def as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def parse_json_list(raw: Any, default: T | None = None) -> list[Any] | T | None:
    if raw is None:
        return default
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return default
    return data if isinstance(data, list) else default


def parse_json_object(raw: Any, default: T | None = None) -> dict[str, Any] | T | None:
    if raw is None:
        return default
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return default
    return data if isinstance(data, dict) else default


def truncate(text: str, limit: int) -> str:
    if limit <= 0 or len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def split_message(text: str, max_len: int = 1900) -> list[str]:
    """Split text into Discord-safe chunks without breaking code fences.

    The splitter prefers line boundaries. When a split happens inside an open
    code block, the chunk gets a closing fence and the next one re-opens it with
    the original language marker.
    """

    if not text:
        return []
    if len(text) <= max_len:
        return [text]

    chunks: list[str] = []
    current = ""
    open_fence: str | None = None

    def flush() -> None:
        nonlocal current
        chunk = current.rstrip("\n")
        if open_fence is not None:
            chunk = f"{chunk}\n```"
        if chunk.strip():
            chunks.append(chunk)
        current = f"{open_fence}\n" if open_fence is not None else ""

    for line in text.splitlines(keepends=True):
        # A single line can be longer than the Discord limit (e.g. minified
        # JSON). Hard-split it while keeping fence bookkeeping intact.
        while len(line) > max_len:
            room = max_len - len(current) - (4 if open_fence else 0)
            if room <= 0:
                flush()
                continue
            piece, line = line[:room], line[room:]
            current += piece
            flush()

        if len(current) + len(line) > max_len:
            flush()
        current += line

        stripped = line.strip()
        if stripped.startswith("```"):
            if open_fence is None:
                open_fence = stripped.split("\n", 1)[0]
            else:
                open_fence = None

    if current.strip():
        chunks.append(current.rstrip("\n"))
    return chunks
