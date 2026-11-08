"""Runtime memory settings stored in ``bot_settings`` (no new env vars)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from discord_bot.utils import as_bool, parse_json_list, safe_int


def _int(
    settings: Mapping[str, str],
    key: str,
    default: int,
    *,
    minimum: int,
    maximum: int,
) -> int:
    return safe_int(settings.get(key), default, minimum=minimum, maximum=maximum)


@dataclass(frozen=True, slots=True)
class MemorySettings:
    enabled: bool = True
    retention_days: int = 365
    max_facts_per_user: int = 200
    session_gap_minutes: int = 30
    worker_poll_seconds: int = 30
    catch_up_limit: int = 100
    max_history: int = 12
    allowed_channel_ids: tuple[str, ...] = ()

    @classmethod
    def from_mapping(cls, settings: Mapping[str, str]) -> MemorySettings:
        raw = parse_json_list(settings.get("allowed_channel_ids"), default=[])
        allowed: tuple[str, ...] = ()
        if isinstance(raw, list):
            allowed = tuple(str(item).strip() for item in raw if str(item).strip())

        return cls(
            enabled=as_bool(settings.get("memory_enabled"), True),
            retention_days=_int(
                settings, "memory_retention_days", 365, minimum=1, maximum=3650
            ),
            max_facts_per_user=_int(
                settings, "max_facts_per_user", 200, minimum=10, maximum=1000
            ),
            session_gap_minutes=_int(
                settings, "session_gap_minutes", 30, minimum=1, maximum=1440
            ),
            worker_poll_seconds=_int(
                settings, "worker_poll_seconds", 30, minimum=5, maximum=3600
            ),
            catch_up_limit=_int(
                settings, "catch_up_limit", 100, minimum=0, maximum=500
            ),
            max_history=_int(settings, "max_history", 12, minimum=1, maximum=100),
            allowed_channel_ids=allowed,
        )

    def channel_allowed(self, channel_id: str, parent_id: str | None = None) -> bool:
        """An empty allowlist means "all channels where the bot is mentioned"."""

        if not self.allowed_channel_ids:
            return True
        if channel_id in self.allowed_channel_ids:
            return True
        return parent_id is not None and parent_id in self.allowed_channel_ids
