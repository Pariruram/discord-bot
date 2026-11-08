"""Supabase implementation of the memory store.

The Supabase Python client is synchronous, so every call runs in a worker
thread through ``asyncio.to_thread`` with a thread-local client. Storage errors
never propagate to the chat pipeline: they are logged and a safe default is
returned, which keeps the bot responsive even before the migration is applied.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Protocol, TypeVar

from discord_bot.memory.models import (
    ConversationState,
    Fact,
    IngestResult,
    Job,
    StoredMessage,
    Summary,
)

logger = logging.getLogger(__name__)

T = TypeVar("T")

FACT_KINDS = {"identity", "preference", "event", "opinion", "relation"}
VISIBILITIES = {"private", "public"}
_ROLE_VALUES = {"user", "assistant", "system"}
_ADDRESSED_VALUES = {"bot", "human", "ambient"}

_SPACE_RE = re.compile(r"\s+")


def _parse_dt(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _message_from_row(row: dict[str, Any]) -> StoredMessage:
    return StoredMessage(
        id=int(row.get("id") or 0),
        discord_message_id=row.get("discord_message_id"),
        author_id=row.get("author_id"),
        author_name=str(row.get("author_name") or "?"),
        is_bot=bool(row.get("is_bot")),
        role=str(row.get("role") or "user"),
        addressed_to=str(row.get("addressed_to") or "ambient"),
        reply_to_id=row.get("reply_to_id"),
        content=str(row.get("content") or ""),
        created_at=_parse_dt(row.get("created_at")),
        session_id=row.get("session_id"),
    )


def _normalise(text: str) -> str:
    return _SPACE_RE.sub(" ", text.strip().lower())


def content_hash(text: str) -> str:
    return hashlib.sha256(_normalise(text).encode("utf-8")).hexdigest()


class MemoryStore(Protocol):
    async def user_memory_enabled(self, user_id: str) -> bool: ...
    async def set_user_memory(self, user_id: str, enabled: bool) -> bool: ...
    async def get_persona(self, user_id: str) -> dict[str, Any]: ...
    async def set_persona(
        self, user_id: str, persona: dict[str, Any], prompt_hint: str | None = None
    ) -> bool: ...
    async def list_facts(
        self,
        user_id: str,
        *,
        include_private: bool,
        guild_id: str | None,
        limit: int = 500,
    ) -> list[Fact]: ...
    async def save_facts(
        self,
        user_id: str,
        *,
        scope: str,
        guild_id: str | None,
        facts: list[Fact],
        source_message_id: int | None,
        source_quote: str | None,
    ) -> int: ...
    async def delete_fact(self, user_id: str, fact_id: str) -> bool: ...
    async def delete_user_facts(self, user_id: str) -> bool: ...
    async def ingest(
        self,
        *,
        scope_type: str,
        scope_id: str,
        guild_id: str | None,
        discord_message_id: str | None,
        author_id: str | None,
        author_name: str,
        is_bot: bool,
        role: str,
        addressed_to: str,
        reply_to_id: str | None,
        content: str,
        session_gap_minutes: int,
        metadata: dict[str, Any] | None = None,
    ) -> IngestResult | None: ...
    async def history_before(
        self, conversation_id: str, before_id: int, limit: int
    ) -> list[StoredMessage]: ...
    async def messages_after(
        self, conversation_id: str, after_id: int, limit: int
    ) -> list[StoredMessage]: ...
    async def session_messages(
        self, session_id: str, limit: int
    ) -> list[StoredMessage]: ...
    async def get_conversation(
        self, conversation_id: str
    ) -> ConversationState | None: ...
    async def list_conversations_to_resume(
        self, limit: int
    ) -> list[ConversationState]: ...
    async def get_open_session(self, conversation_id: str) -> dict[str, Any] | None: ...
    async def list_unsummarized_sessions(
        self, conversation_id: str, limit: int = 5
    ) -> list[dict[str, Any]]: ...
    async def close_session(self, session_id: str, ended_at: datetime | None) -> bool: ...
    async def save_session_summary(
        self,
        *,
        conversation_id: str,
        session_id: str,
        content: str,
        covers_from: datetime | None,
        covers_to: datetime | None,
    ) -> bool: ...
    async def save_conversation_summary(
        self,
        *,
        conversation_id: str,
        content: str,
        covers_from: datetime | None,
        covers_to: datetime | None,
    ) -> bool: ...
    async def list_summaries(
        self, conversation_id: str, limit: int
    ) -> list[Summary]: ...
    async def latest_conversation_summary(
        self, conversation_id: str
    ) -> Summary | None: ...
    async def count_session_summaries_since(
        self, conversation_id: str, since: datetime | None
    ) -> int: ...
    async def set_markers(
        self,
        conversation_id: str,
        *,
        summary_message_id: int | None = None,
        extracted_message_id: int | None = None,
        summary_at: datetime | None = None,
    ) -> bool: ...
    async def enqueue_job(
        self,
        kind: str,
        conversation_id: str,
        *,
        not_before: datetime | None = None,
    ) -> bool: ...
    async def claim_jobs(self, limit: int) -> list[Job]: ...
    async def finish_job(self, job_id: int) -> bool: ...
    async def fail_job(self, job_id: int, error: str) -> bool: ...
    async def requeue_stale_jobs(self) -> bool: ...
    async def forget_message(self, discord_message_id: str) -> bool: ...
    async def update_message(self, discord_message_id: str, content: str) -> bool: ...
    async def purge_old_messages(self, older_than: datetime) -> int: ...
    async def purge_old_facts(self, older_than: datetime) -> int: ...
    async def close(self) -> None: ...


class SupabaseMemoryRepository:
    def __init__(
        self,
        url: str,
        key: str,
        *,
        max_concurrency: int = 8,
        timeout: float = 15.0,
    ) -> None:
        self._url = url
        self._key = key
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._timeout = timeout
        self._local = threading.local()

    def _client(self):
        client = getattr(self._local, "client", None)
        if client is None:
            from supabase import create_client

            client = create_client(self._url, self._key)
            self._local.client = client
        return client

    async def _call(
        self,
        operation: str,
        fn: Callable[[], T],
        *,
        default: T,
    ) -> T:
        try:
            async with self._semaphore:
                return await asyncio.wait_for(
                    asyncio.to_thread(fn),
                    timeout=self._timeout,
                )
        except TimeoutError:
            logger.error("%s: таймаут %.1fs", operation, self._timeout)
        except Exception:  # noqa: BLE001 - storage failures must not kill the bot
            logger.exception("%s: ошибка", operation)
        return default

    @staticmethod
    def _rows(response: Any) -> list[dict[str, Any]]:
        data = getattr(response, "data", None)
        return data if isinstance(data, list) else []

    async def user_memory_enabled(self, user_id: str) -> bool:
        def fetch() -> bool:
            response = (
                self._client()
                .table("users")
                .select("memory_enabled")
                .eq("user_id", user_id)
                .execute()
            )
            data = self._rows(response)
            if not data:
                return True
            return bool(data[0].get("memory_enabled", True))

        return await self._call(
            f"user_memory_enabled({user_id})", fetch, default=True
        )

    async def set_user_memory(self, user_id: str, enabled: bool) -> bool:
        def write() -> bool:
            self._client().table("users").upsert(
                {"user_id": user_id, "memory_enabled": enabled},
                on_conflict="user_id",
            ).execute()
            return True

        return await self._call(
            f"set_user_memory({user_id},{enabled})", write, default=False
        )

    async def get_persona(self, user_id: str) -> dict[str, Any]:
        def fetch() -> dict[str, Any]:
            response = (
                self._client()
                .table("user_personas")
                .select("persona, prompt_hint")
                .eq("user_id", user_id)
                .execute()
            )
            data = self._rows(response)
            if not data:
                return {}
            row = data[0]
            persona = row.get("persona")
            return {
                "persona": persona if isinstance(persona, dict) else {},
                "prompt_hint": row.get("prompt_hint") or "",
            }

        return await self._call(
            f"get_persona({user_id})", fetch, default={}
        )

    async def set_persona(
        self, user_id: str, persona: dict[str, Any], prompt_hint: str | None = None
    ) -> bool:
        def write() -> bool:
            client = self._client()
            client.table("users").upsert(
                {"user_id": user_id}, on_conflict="user_id"
            ).execute()
            client.table("user_personas").upsert(
                {
                    "user_id": user_id,
                    "persona": persona,
                    "prompt_hint": prompt_hint or "",
                    "updated_at": datetime.now(UTC).isoformat(),
                },
                on_conflict="user_id",
            ).execute()
            return True

        return await self._call(f"set_persona({user_id})", write, default=False)

    async def list_facts(
        self,
        user_id: str,
        *,
        include_private: bool,
        guild_id: str | None,
        limit: int = 500,
    ) -> list[Fact]:
        def fetch() -> list[Fact]:
            response = (
                self._client()
                .table("user_facts")
                .select("id, kind, content, visibility, confidence, importance, scope, guild_id")
                .eq("user_id", user_id)
                .is_("valid_to", "null")
                .order("importance", desc=True)
                .order("last_confirmed_at", desc=True)
                .limit(limit)
                .execute()
            )
            facts: list[Fact] = []
            for row in self._rows(response):
                scope = str(row.get("scope") or "global")
                if not include_private:
                    if str(row.get("visibility") or "private") != "public":
                        continue
                    if scope == "guild" and (
                        guild_id is None or row.get("guild_id") not in (None, guild_id)
                    ):
                        continue
                facts.append(
                    Fact(
                        id=str(row.get("id")) if row.get("id") else None,
                        kind=str(row.get("kind") or "identity"),
                        content=str(row.get("content") or ""),
                        visibility=str(row.get("visibility") or "private"),
                        confidence=float(row.get("confidence") or 0.7),
                        importance=float(row.get("importance") or 0.5),
                    )
                )
            return facts

        return await self._call(
            f"list_facts({user_id})", fetch, default=[]
        )

    async def save_facts(
        self,
        user_id: str,
        *,
        scope: str,
        guild_id: str | None,
        facts: list[Fact],
        source_message_id: int | None,
        source_quote: str | None,
    ) -> int:
        if not facts:
            return 0

        def write() -> int:
            client = self._client()
            now = datetime.now(UTC).isoformat()
            saved = 0
            for fact in facts:
                content = fact.content.strip()
                if not content:
                    continue
                kind = fact.kind if fact.kind in FACT_KINDS else "identity"
                visibility = (
                    fact.visibility if fact.visibility in VISIBILITIES else "private"
                )
                try:
                    client.table("user_facts").upsert(
                        {
                            "user_id": user_id,
                            "scope": scope if scope in {"global", "guild"} else "global",
                            "guild_id": guild_id if scope == "guild" else None,
                            "kind": kind,
                            "content": content,
                            "content_hash": content_hash(content),
                            "confidence": max(0.0, min(1.0, fact.confidence)),
                            "importance": max(0.0, min(1.0, fact.importance)),
                            "visibility": visibility,
                            "source_message_id": source_message_id,
                            "source_quote": (source_quote or content)[:1000],
                            "last_confirmed_at": now,
                        },
                        on_conflict="user_id,content_hash",
                        ignore_duplicates=True,
                    ).execute()
                    saved += 1
                except Exception:  # noqa: BLE001 - one bad fact must not block the rest
                    logger.exception("Не удалось сохранить факт для %s", user_id)
            return saved

        return await self._call(
            f"save_facts({user_id})", write, default=0
        )

    async def delete_fact(self, user_id: str, fact_id: str) -> bool:
        def write() -> bool:
            self._client().table("user_facts").delete().eq(
                "user_id", user_id
            ).eq("id", fact_id).execute()
            return True

        return await self._call(
            f"delete_fact({user_id},{fact_id})", write, default=False
        )

    async def delete_user_facts(self, user_id: str) -> bool:
        def write() -> bool:
            self._client().table("user_facts").delete().eq(
                "user_id", user_id
            ).execute()
            return True

        return await self._call(
            f"delete_user_facts({user_id})", write, default=False
        )

    async def ingest(
        self,
        *,
        scope_type: str,
        scope_id: str,
        guild_id: str | None,
        discord_message_id: str | None,
        author_id: str | None,
        author_name: str,
        is_bot: bool,
        role: str,
        addressed_to: str,
        reply_to_id: str | None,
        content: str,
        session_gap_minutes: int,
        metadata: dict[str, Any] | None = None,
    ) -> IngestResult | None:
        def write() -> IngestResult | None:
            response = self._client().rpc(
                "ingest_message",
                {
                    "p_scope_type": scope_type,
                    "p_scope_id": scope_id,
                    "p_guild_id": guild_id,
                    "p_discord_message_id": discord_message_id,
                    "p_author_id": author_id,
                    "p_author_name": author_name,
                    "p_is_bot": is_bot,
                    "p_role": role if role in _ROLE_VALUES else "user",
                    "p_addressed_to": (
                        addressed_to if addressed_to in _ADDRESSED_VALUES else "ambient"
                    ),
                    "p_reply_to_id": reply_to_id,
                    "p_content": content,
                    "p_session_gap_minutes": session_gap_minutes,
                    "p_metadata": metadata or {},
                },
            ).execute()
            rows = self._rows(response)
            if not rows:
                return None
            row = rows[0]
            return IngestResult(
                message_id=int(row.get("message_id") or 0),
                conversation_id=str(row.get("conversation_id") or ""),
                session_id=row.get("session_id"),
                duplicate=bool(row.get("duplicate")),
            )

        return await self._call(
            f"ingest({scope_type}:{scope_id})", write, default=None
        )

    async def history_before(
        self, conversation_id: str, before_id: int, limit: int
    ) -> list[StoredMessage]:
        def fetch() -> list[StoredMessage]:
            response = self._client().rpc(
                "history_before",
                {
                    "p_conversation_id": conversation_id,
                    "p_before_id": before_id,
                    "p_limit": limit,
                },
            ).execute()
            return [_message_from_row(row) for row in self._rows(response)]

        return await self._call(
            f"history_before({conversation_id})", fetch, default=[]
        )

    async def messages_after(
        self, conversation_id: str, after_id: int, limit: int
    ) -> list[StoredMessage]:
        def fetch() -> list[StoredMessage]:
            response = (
                self._client()
                .table("messages")
                .select(
                    "id, discord_message_id, author_id, author_name, is_bot, role, "
                    "addressed_to, reply_to_id, content, created_at, session_id"
                )
                .eq("conversation_id", conversation_id)
                .gt("id", after_id)
                .is_("deleted_at", "null")
                .order("id")
                .limit(limit)
                .execute()
            )
            return [_message_from_row(row) for row in self._rows(response)]

        return await self._call(
            f"messages_after({conversation_id})", fetch, default=[]
        )

    async def session_messages(
        self, session_id: str, limit: int
    ) -> list[StoredMessage]:
        def fetch() -> list[StoredMessage]:
            response = (
                self._client()
                .table("messages")
                .select(
                    "id, discord_message_id, author_id, author_name, is_bot, role, "
                    "addressed_to, reply_to_id, content, created_at, session_id"
                )
                .eq("session_id", session_id)
                .is_("deleted_at", "null")
                .order("id")
                .limit(limit)
                .execute()
            )
            return [_message_from_row(row) for row in self._rows(response)]

        return await self._call(
            f"session_messages({session_id})", fetch, default=[]
        )

    async def get_conversation(
        self, conversation_id: str
    ) -> ConversationState | None:
        def fetch() -> ConversationState | None:
            response = (
                self._client()
                .table("conversations")
                .select("*")
                .eq("id", conversation_id)
                .execute()
            )
            rows = self._rows(response)
            if not rows:
                return None
            return self._conversation_from_row(rows[0])

        return await self._call(
            f"get_conversation({conversation_id})", fetch, default=None
        )

    @staticmethod
    def _conversation_from_row(row: dict[str, Any]) -> ConversationState:
        return ConversationState(
            id=str(row.get("id")),
            scope_type=str(row.get("scope_type") or "channel"),
            scope_id=str(row.get("scope_id") or ""),
            guild_id=row.get("guild_id"),
            last_message_at=_parse_dt(row.get("last_message_at")),
            last_seen_message_id=row.get("last_seen_message_id"),
            last_summary_message_id=int(row.get("last_summary_message_id") or 0),
            last_extracted_message_id=int(
                row.get("last_extracted_message_id") or 0
            ),
        )

    async def list_conversations_to_resume(
        self, limit: int
    ) -> list[ConversationState]:
        def fetch() -> list[ConversationState]:
            response = (
                self._client()
                .table("conversations")
                .select("*")
                .not_.is_("last_seen_message_id", "null")
                .order("last_seen_at", desc=True)
                .limit(limit)
                .execute()
            )
            return [
                self._conversation_from_row(row)
                for row in self._rows(response)
            ]

        return await self._call(
            "list_conversations_to_resume", fetch, default=[]
        )

    async def get_open_session(
        self, conversation_id: str
    ) -> dict[str, Any] | None:
        def fetch() -> dict[str, Any] | None:
            response = (
                self._client()
                .table("conversation_sessions")
                .select("id, started_at, message_count, summary")
                .eq("conversation_id", conversation_id)
                .is_("ended_at", "null")
                .order("started_at", desc=True)
                .limit(1)
                .execute()
            )
            rows = self._rows(response)
            if not rows:
                return None
            session = dict(rows[0])
            session["started_at"] = _parse_dt(session.get("started_at"))
            return session

        return await self._call(
            f"get_open_session({conversation_id})", fetch, default=None
        )

    async def list_unsummarized_sessions(
        self, conversation_id: str, limit: int = 5
    ) -> list[dict[str, Any]]:
        """Sessions closed by the ingest RPC during a long gap.

        ``ingest_message`` rotates sessions when the gap exceeded the limit, but
        only the worker can call an LLM. These rows are summarised on the next
        maintenance pass so no conversation fragment is silently lost.
        """

        def fetch() -> list[dict[str, Any]]:
            response = (
                self._client()
                .table("conversation_sessions")
                .select("id, started_at, ended_at")
                .eq("conversation_id", conversation_id)
                .not_.is_("ended_at", "null")
                .is_("summary", "null")
                .order("started_at")
                .limit(max(limit, 1))
                .execute()
            )
            sessions: list[dict[str, Any]] = []
            for row in self._rows(response):
                session = dict(row)
                session["started_at"] = _parse_dt(session.get("started_at"))
                session["ended_at"] = _parse_dt(session.get("ended_at"))
                sessions.append(session)
            return sessions

        return await self._call(
            f"list_unsummarized_sessions({conversation_id})", fetch, default=[]
        )

    async def close_session(
        self, session_id: str, ended_at: datetime | None
    ) -> bool:
        def write() -> bool:
            self._client().table("conversation_sessions").update(
                {"ended_at": (ended_at or datetime.now(UTC)).isoformat()}
            ).eq("id", session_id).execute()
            return True

        return await self._call(f"close_session({session_id})", write, default=False)

    async def save_session_summary(
        self,
        *,
        conversation_id: str,
        session_id: str,
        content: str,
        covers_from: datetime | None,
        covers_to: datetime | None,
    ) -> bool:
        def write() -> bool:
            client = self._client()
            client.table("conversation_summaries").insert(
                {
                    "conversation_id": conversation_id,
                    "session_id": session_id,
                    "level": "session",
                    "content": content,
                    "covers_from": covers_from.isoformat() if covers_from else None,
                    "covers_to": covers_to.isoformat() if covers_to else None,
                }
            ).execute()
            client.table("conversation_sessions").update({"summary": content}).eq(
                "id", session_id
            ).execute()
            return True

        return await self._call(
            f"save_session_summary({session_id})", write, default=False
        )

    async def save_conversation_summary(
        self,
        *,
        conversation_id: str,
        content: str,
        covers_from: datetime | None,
        covers_to: datetime | None,
    ) -> bool:
        def write() -> bool:
            self._client().table("conversation_summaries").insert(
                {
                    "conversation_id": conversation_id,
                    "level": "conversation",
                    "content": content,
                    "covers_from": covers_from.isoformat() if covers_from else None,
                    "covers_to": covers_to.isoformat() if covers_to else None,
                }
            ).execute()
            return True

        return await self._call(
            f"save_conversation_summary({conversation_id})", write, default=False
        )

    async def list_summaries(
        self, conversation_id: str, limit: int
    ) -> list[Summary]:
        def fetch() -> list[Summary]:
            response = (
                self._client()
                .table("conversation_summaries")
                .select("level, content, created_at")
                .eq("conversation_id", conversation_id)
                .order("created_at", desc=True)
                .limit(limit)
                .execute()
            )
            rows = [
                Summary(
                    level=str(row.get("level") or "session"),
                    content=str(row.get("content") or ""),
                    created_at=_parse_dt(row.get("created_at")),
                )
                for row in self._rows(response)
            ]
            return list(reversed(rows))

        return await self._call(
            f"list_summaries({conversation_id})", fetch, default=[]
        )

    async def latest_conversation_summary(
        self, conversation_id: str
    ) -> Summary | None:
        def fetch() -> Summary | None:
            response = (
                self._client()
                .table("conversation_summaries")
                .select("level, content, created_at")
                .eq("conversation_id", conversation_id)
                .eq("level", "conversation")
                .order("created_at", desc=True)
                .limit(1)
                .execute()
            )
            rows = self._rows(response)
            if not rows:
                return None
            row = rows[0]
            return Summary(
                level="conversation",
                content=str(row.get("content") or ""),
                created_at=_parse_dt(row.get("created_at")),
            )

        return await self._call(
            f"latest_conversation_summary({conversation_id})", fetch, default=None
        )

    async def count_session_summaries_since(
        self, conversation_id: str, since: datetime | None
    ) -> int:
        def fetch() -> int:
            query = (
                self._client()
                .table("conversation_summaries")
                .select("id")
                .eq("conversation_id", conversation_id)
                .eq("level", "session")
            )
            if since is not None:
                query = query.gt("created_at", since.isoformat())
            response = query.execute()
            return len(self._rows(response))

        return await self._call(
            f"count_session_summaries_since({conversation_id})",
            fetch,
            default=0,
        )

    async def set_markers(
        self,
        conversation_id: str,
        *,
        summary_message_id: int | None = None,
        extracted_message_id: int | None = None,
        summary_at: datetime | None = None,
    ) -> bool:
        payload: dict[str, Any] = {}
        if summary_message_id is not None:
            payload["last_summary_message_id"] = summary_message_id
        if extracted_message_id is not None:
            payload["last_extracted_message_id"] = extracted_message_id
        if summary_at is not None:
            payload["summary_at"] = summary_at.isoformat()
        if not payload:
            return True

        def write() -> bool:
            self._client().table("conversations").update(payload).eq(
                "id", conversation_id
            ).execute()
            return True

        return await self._call(
            f"set_markers({conversation_id})", write, default=False
        )

    async def enqueue_job(
        self,
        kind: str,
        conversation_id: str,
        *,
        not_before: datetime | None = None,
    ) -> bool:
        def write() -> bool:
            self._client().rpc(
                "enqueue_memory_job",
                {
                    "p_kind": kind,
                    "p_conversation_id": conversation_id,
                    "p_target_id": 0,
                    "p_not_before": (not_before or datetime.now(UTC)).isoformat(),
                    "p_payload": {},
                },
            ).execute()
            return True

        return await self._call(
            f"enqueue_job({kind})", write, default=False
        )

    async def claim_jobs(self, limit: int) -> list[Job]:
        def fetch() -> list[Job]:
            response = self._client().rpc(
                "claim_memory_jobs", {"p_batch": limit}
            ).execute()
            jobs: list[Job] = []
            for row in self._rows(response):
                jobs.append(
                    Job(
                        id=int(row.get("id") or 0),
                        kind=str(row.get("kind") or "maintenance"),
                        conversation_id=row.get("conversation_id"),
                        target_id=int(row.get("target_id") or 0),
                        payload=row.get("payload") or {},
                    )
                )
            return jobs

        return await self._call("claim_jobs", fetch, default=[])

    async def finish_job(self, job_id: int) -> bool:
        def write() -> bool:
            self._client().rpc(
                "finish_memory_job", {"p_id": job_id}
            ).execute()
            return True

        return await self._call(f"finish_job({job_id})", write, default=False)

    async def fail_job(self, job_id: int, error: str) -> bool:
        def write() -> bool:
            self._client().rpc(
                "fail_memory_job",
                {
                    "p_id": job_id,
                    "p_error": error,
                    "p_delay_seconds": 300,
                },
            ).execute()
            return True

        return await self._call(f"fail_job({job_id})", write, default=False)

    async def requeue_stale_jobs(self) -> bool:
        def write() -> bool:
            self._client().rpc("requeue_stale_jobs", {}).execute()
            return True

        return await self._call("requeue_stale_jobs", write, default=False)

    async def forget_message(self, discord_message_id: str) -> bool:
        def write() -> bool:
            self._client().rpc(
                "soft_delete_message",
                {"p_discord_message_id": discord_message_id},
            ).execute()
            return True

        return await self._call(
            f"forget_message({discord_message_id})", write, default=False
        )

    async def update_message(self, discord_message_id: str, content: str) -> bool:
        def write() -> bool:
            self._client().rpc(
                "update_message_content",
                {
                    "p_discord_message_id": discord_message_id,
                    "p_content": content,
                },
            ).execute()
            return True

        return await self._call(
            f"update_message({discord_message_id})", write, default=False
        )

    async def close(self) -> None:
        """Nothing to release: the sync client owns no long-lived resources."""

    # ------------------------------------------------------------------
    # Retention helpers (used by the worker once a day)
    # ------------------------------------------------------------------

    async def purge_old_messages(self, older_than: datetime) -> int:
        def write() -> int:
            response = (
                self._client()
                .table("messages")
                .delete()
                .lt("created_at", older_than.isoformat())
                .execute()
            )
            return len(self._rows(response))

        return await self._call("purge_old_messages", write, default=0)

    async def purge_old_facts(self, older_than: datetime) -> int:
        def write() -> int:
            response = (
                self._client()
                .table("user_facts")
                .delete()
                .lt("valid_to", older_than.isoformat())
                .execute()
            )
            return len(self._rows(response))

        return await self._call("purge_old_facts", write, default=0)
