"""TTL cache for bot settings to avoid a database round-trip per message."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Mapping

from bot.storage.repository import Repository
from bot.utils import parse_json_list

logger = logging.getLogger(__name__)


class SettingsCache:
    def __init__(self, repo: Repository, *, ttl: float = 60.0) -> None:
        self._repo = repo
        self._ttl = ttl
        self._data: dict[str, str] | None = None
        self._loaded_at = 0.0
        self._lock = asyncio.Lock()

    def invalidate(self) -> None:
        self._data = None
        self._loaded_at = 0.0

    async def get_all(self) -> dict[str, str]:
        now = time.monotonic()
        if self._data is not None and now - self._loaded_at < self._ttl:
            return dict(self._data)

        async with self._lock:
            now = time.monotonic()
            if self._data is not None and now - self._loaded_at < self._ttl:
                return dict(self._data)

            data = await self._repo.get_all_settings()
            self._data = dict(data)
            self._loaded_at = time.monotonic()
            return dict(self._data)

    async def get(self, key: str, default: str = "") -> str:
        return (await self.get_all()).get(key, default)

    async def set(self, key: str, value: str) -> bool:
        async with self._lock:
            ok = await self._repo.set_setting(key, value)
            if self._data is not None:
                self._data[key] = value
            else:
                # Nothing loaded yet: fetch the full snapshot on the next read
                # instead of exposing a cache that contains only this key.
                self.invalidate()
            return ok

    async def update(self, values: Mapping[str, str]) -> bool:
        """Persist and apply several settings as one cache update.

        Readers keep seeing the old snapshot until all writes finish, so they
        cannot observe a half-applied provider/model combination.
        """

        async with self._lock:
            results: list[bool] = []
            for key, value in values.items():
                results.append(await self._repo.set_setting(key, value))

            if self._data is not None:
                self._data = {**self._data, **values}
            else:
                self.invalidate()
            return all(results)

    async def get_owners(self) -> list[str]:
        raw = await self.get("owners", "[]")
        owners = parse_json_list(raw, default=[])
        if not isinstance(owners, list):
            return []
        return [str(owner) for owner in owners]

    async def save_owners(self, owners: list[str]) -> bool:
        return await self.set("owners", json.dumps([str(owner) for owner in owners]))

    async def snapshot(self) -> Mapping[str, str]:
        return await self.get_all()
