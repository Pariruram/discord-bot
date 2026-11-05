"""Supabase repository.

The Supabase Python client is synchronous. Rather than depending on a specific
async-client API version, every call is executed in a worker thread. A
thread-local client avoids sharing SDK/HTTP state between worker threads.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Protocol, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


class Repository(Protocol):
    async def get_all_settings(self) -> dict[str, str]: ...
    async def get_setting(self, key: str, default: str = "") -> str: ...
    async def set_setting(self, key: str, value: str) -> bool: ...
    async def get_user_profile(self, user_id: str) -> dict[str, Any]: ...
    async def update_preferences(self, user_id: str, preferences: dict[str, Any]) -> bool: ...
    async def fetch_recent_history(self, user_id: str, limit: int) -> list[dict[str, str]]: ...
    async def save_message(self, user_id: str, role: str, content: str) -> bool: ...
    async def load_channel_context(self, channel_id: str) -> dict[str, Any]: ...
    async def save_channel_context(
        self, channel_id: str, summary: str, messages: list[dict[str, str]]
    ) -> bool: ...
    async def close(self) -> None: ...


class SupabaseRepository:
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

    async def get_all_settings(self) -> dict[str, str]:
        def fetch() -> dict[str, str]:
            response = self._client().table("bot_settings").select("key, value").execute()
            return {row["key"]: row["value"] for row in response.data}

        return await self._call("get_all_settings", fetch, default={})

    async def get_setting(self, key: str, default: str = "") -> str:
        def fetch() -> str:
            response = (
                self._client()
                .table("bot_settings")
                .select("value")
                .eq("key", key)
                .execute()
            )
            if response.data:
                return str(response.data[0]["value"])
            return default

        return await self._call(f"get_setting({key})", fetch, default=default)

    async def set_setting(self, key: str, value: str) -> bool:
        def write() -> bool:
            self._client().table("bot_settings").upsert(
                {
                    "key": key,
                    "value": value,
                    "updated_at": datetime.now(UTC).isoformat(),
                }
            ).execute()
            return True

        return await self._call(f"set_setting({key})", write, default=False)

    async def get_user_profile(self, user_id: str) -> dict[str, Any]:
        def fetch() -> dict[str, Any]:
            response = (
                self._client()
                .table("user_profiles")
                .select("*")
                .eq("user_id", user_id)
                .execute()
            )
            if response.data:
                row = dict(response.data[0])
                if not isinstance(row.get("preferences"), dict):
                    row["preferences"] = {}
                return row

            self._client().table("user_profiles").insert(
                {"user_id": user_id, "preferences": {}}
            ).execute()
            return {"user_id": user_id, "preferences": {}}

        return await self._call(
            f"get_user_profile({user_id})",
            fetch,
            default={"user_id": user_id, "preferences": {}},
        )

    async def update_preferences(
        self, user_id: str, preferences: dict[str, Any]
    ) -> bool:
        def write() -> bool:
            self._client().table("user_profiles").upsert(
                {
                    "user_id": user_id,
                    "preferences": preferences,
                    "updated_at": datetime.now(UTC).isoformat(),
                }
            ).execute()
            return True

        return await self._call(
            f"update_preferences({user_id})", write, default=False
        )

    async def fetch_recent_history(
        self, user_id: str, limit: int
    ) -> list[dict[str, str]]:
        def fetch() -> list[dict[str, str]]:
            response = (
                self._client()
                .table("chat_history")
                .select("role, content")
                .eq("user_id", user_id)
                .order("created_at", desc=True)
                .limit(limit)
                .execute()
            )
            rows = [
                {"role": row["role"], "content": row["content"]}
                for row in response.data
            ]
            return list(reversed(rows))

        return await self._call(
            f"fetch_recent_history({user_id})", fetch, default=[]
        )

    async def save_message(self, user_id: str, role: str, content: str) -> bool:
        def write() -> bool:
            self._client().table("chat_history").insert(
                {"user_id": user_id, "role": role, "content": content}
            ).execute()
            return True

        return await self._call(f"save_message({user_id},{role})", write, default=False)

    async def load_channel_context(self, channel_id: str) -> dict[str, Any]:
        def fetch() -> dict[str, Any]:
            response = (
                self._client()
                .table("channel_context")
                .select("summary, messages")
                .eq("channel_id", channel_id)
                .execute()
            )
            if not response.data:
                return {"summary": "", "messages": []}
            row = dict(response.data[0])
            messages = row.get("messages")
            return {
                "summary": row.get("summary") or "",
                "messages": messages if isinstance(messages, list) else [],
            }

        return await self._call(
            f"load_channel_context({channel_id})",
            fetch,
            default={"summary": "", "messages": []},
        )

    async def save_channel_context(
        self, channel_id: str, summary: str, messages: list[dict[str, str]]
    ) -> bool:
        def write() -> bool:
            self._client().table("channel_context").upsert(
                {
                    "channel_id": channel_id,
                    "summary": summary,
                    "messages": messages,
                    "updated_at": datetime.now(UTC).isoformat(),
                }
            ).execute()
            return True

        return await self._call(
            f"save_channel_context({channel_id})", write, default=False
        )

    async def close(self) -> None:
        """The sync client holds no resources that require explicit cleanup."""
