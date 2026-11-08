"""Supabase repository for bot settings.

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
from typing import Protocol, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


class Repository(Protocol):
    async def get_all_settings(self) -> dict[str, str]: ...
    async def set_setting(self, key: str, value: str) -> bool: ...
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

    async def close(self) -> None:
        """The sync client holds no resources that require explicit cleanup."""
