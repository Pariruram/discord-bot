"""Plain data structures shared by the memory layer."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class StoredMessage:
    id: int
    discord_message_id: str | None
    author_id: str | None
    author_name: str
    is_bot: bool
    role: str
    addressed_to: str
    reply_to_id: str | None
    content: str
    created_at: datetime | None
    session_id: str | None


@dataclass(frozen=True, slots=True)
class ConversationState:
    id: str
    scope_type: str
    scope_id: str
    guild_id: str | None
    last_message_at: datetime | None
    last_seen_message_id: str | None
    last_summary_message_id: int = 0
    last_extracted_message_id: int = 0


@dataclass(frozen=True, slots=True)
class IngestResult:
    message_id: int
    conversation_id: str
    session_id: str | None
    duplicate: bool = False


@dataclass(frozen=True, slots=True)
class Fact:
    id: str | None
    kind: str
    content: str
    visibility: str = "private"
    confidence: float = 0.7
    importance: float = 0.5


@dataclass(frozen=True, slots=True)
class Summary:
    level: str
    content: str
    created_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class Job:
    id: int
    kind: str
    conversation_id: str | None
    target_id: int = 0
    payload: dict | None = None
