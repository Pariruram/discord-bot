from __future__ import annotations

from discord_bot.memory.settings import MemorySettings


def test_defaults_do_not_require_new_env_vars() -> None:
    cfg = MemorySettings.from_mapping({})

    assert cfg.enabled is True
    assert cfg.retention_days == 365
    assert cfg.max_facts_per_user == 200
    assert cfg.session_gap_minutes == 30
    assert cfg.worker_poll_seconds == 30
    assert cfg.catch_up_limit == 100
    assert cfg.max_history == 12
    assert cfg.allowed_channel_ids == ()


def test_values_are_read_from_bot_settings() -> None:
    cfg = MemorySettings.from_mapping(
        {
            "memory_enabled": "false",
            "memory_retention_days": "30",
            "max_facts_per_user": "50",
            "session_gap_minutes": "10",
            "worker_poll_seconds": "15",
            "catch_up_limit": "0",
            "max_history": "20",
            "allowed_channel_ids": '["11", "22"]',
        }
    )

    assert cfg.enabled is False
    assert cfg.retention_days == 30
    assert cfg.max_facts_per_user == 50
    assert cfg.session_gap_minutes == 10
    assert cfg.worker_poll_seconds == 15
    assert cfg.catch_up_limit == 0
    assert cfg.max_history == 20
    assert cfg.allowed_channel_ids == ("11", "22")


def test_invalid_numbers_fall_back_to_defaults() -> None:
    cfg = MemorySettings.from_mapping(
        {
            "memory_retention_days": "not-a-number",
            "session_gap_minutes": "-5",
        }
    )

    assert cfg.retention_days == 365
    assert cfg.session_gap_minutes == 1


def test_empty_allowlist_allows_every_channel() -> None:
    cfg = MemorySettings.from_mapping({})

    assert cfg.channel_allowed("123")


def test_allowlist_matches_channel_or_thread_parent() -> None:
    cfg = MemorySettings.from_mapping({"allowed_channel_ids": '["123"]'})

    assert cfg.channel_allowed("123")
    assert cfg.channel_allowed("999", parent_id="123")
    assert not cfg.channel_allowed("999")
