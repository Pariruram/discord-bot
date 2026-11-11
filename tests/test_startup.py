from __future__ import annotations

import json
import time
from datetime import UTC, datetime, timedelta

import discord
import pytest

from discord_bot.health import HealthState
from discord_bot.main import (
    LOGIN_GATE_KEY,
    _close_login_session,
    _start_bot,
    _token_fingerprint,
    _wait_for_login_gate,
    _wait_for_previous_instance,
)


class RateLimited(discord.HTTPException):
    status = 429
    code = 0
    text = "Too Many Requests"

    def __init__(self, retry_after: int | None = None) -> None:
        self.retry_after = retry_after


class Forbidden(discord.HTTPException):
    status = 403
    code = 0
    text = "Forbidden"

    def __init__(self) -> None:
        pass


class FakeHTTP:
    def __init__(self) -> None:
        self.connector = discord.utils.MISSING
        self.closed = 0

    async def close(self) -> None:
        self.closed += 1


class FakeBot:
    def __init__(self, failures: list[Exception]) -> None:
        self.failures = list(failures)
        self.calls = 0
        self.http = FakeHTTP()

    async def start(self, token: str) -> None:
        self.calls += 1
        if self.failures:
            raise self.failures.pop(0)


async def test_start_retries_on_429_and_honours_retry_after() -> None:
    bot = FakeBot([RateLimited(3600)])
    waits: list[float] = []

    async def sleep(seconds: float) -> None:
        waits.append(seconds)

    await _start_bot(bot, "token", sleep=sleep)

    assert bot.calls == 2
    assert waits == [3600]
    assert bot.http.closed == 1
    assert bot.http.connector is discord.utils.MISSING


async def test_start_backs_off_without_discord_retry_after() -> None:
    waits = []

    async def sleep(seconds: float) -> None:
        waits.append(seconds)

    bot = FakeBot([RateLimited(), RateLimited(), RateLimited(), RateLimited()])
    await _start_bot(bot, "token", sleep=sleep)

    assert bot.calls == 5
    assert waits == [300, 900, 1800, 3600]


async def test_backoff_is_capped_at_one_hour() -> None:
    waits = []

    async def sleep(seconds: float) -> None:
        waits.append(seconds)

    failures = [RateLimited()] * 4
    bot = FakeBot(failures)
    await _start_bot(bot, "token", sleep=sleep)

    assert waits[-1] == 3600
    assert all(wait <= 3600 for wait in waits)


async def test_start_uses_a_five_minute_floor() -> None:
    bot = FakeBot([RateLimited(2)])
    waits: list[float] = []

    async def sleep(seconds: float) -> None:
        waits.append(seconds)

    await _start_bot(bot, "token", sleep=sleep)

    assert waits == [300]


async def test_start_does_not_retry_other_http_errors() -> None:
    bot = FakeBot([Forbidden()])

    with pytest.raises(discord.HTTPException):
        await _start_bot(bot, "token", sleep=lambda seconds: None)


async def test_start_tolerates_a_bot_without_http() -> None:
    bot = FakeBot([RateLimited(1)])
    del bot.http

    assert await _close_login_session(bot) is None

    waits: list[float] = []

    async def sleep(seconds: float) -> None:
        waits.append(seconds)

    await _start_bot(bot, "token", sleep=sleep)

    assert bot.calls == 2


async def test_close_login_session_swallows_close_errors() -> None:
    class BrokenHTTP:
        connector = discord.utils.MISSING

        async def close(self) -> None:
            raise TimeoutError

    bot = FakeBot([])
    bot.http = BrokenHTTP()

    await _close_login_session(bot)

    assert bot.http.connector is discord.utils.MISSING


class FakeSettings:
    def __init__(self, data: dict[str, str] | None = None) -> None:
        self.data = dict(data or {})
        self.writes: list[tuple[str, str]] = []

    async def get(self, key: str, default: str = "") -> str:
        return self.data.get(key, default)

    async def set(self, key: str, value: str) -> None:
        self.data[key] = value
        self.writes.append((key, value))


def gate_payload(token: str, until: float) -> str:
    return json.dumps({"until": until, "token": _token_fingerprint(token)})


async def test_login_gate_is_honoured_across_restarts() -> None:
    settings = FakeSettings({LOGIN_GATE_KEY: gate_payload("token", 1030.0)})
    health = HealthState(instance_id="new")
    waits: list[float] = []

    async def sleep(seconds: float) -> None:
        waits.append(seconds)

    await _wait_for_login_gate(
        settings,
        "token",
        health,
        sleep=sleep,
        clock=lambda: 1000.0,
    )

    assert waits == [30.0]
    assert health.login_state == "waiting_login"


async def test_login_gate_ignores_a_different_token() -> None:
    settings = FakeSettings({LOGIN_GATE_KEY: gate_payload("old-token", 1030.0)})
    waits: list[float] = []

    async def sleep(seconds: float) -> None:
        waits.append(seconds)

    await _wait_for_login_gate(
        settings,
        "new-token",
        sleep=sleep,
        clock=lambda: 1000.0,
    )

    assert waits == []


async def test_start_bot_persists_a_429_deadline() -> None:
    settings = FakeSettings()
    bot = FakeBot([RateLimited(120)])

    async def sleep(seconds: float) -> None:
        return None

    await _start_bot(bot, "token", sleep=sleep, settings=settings)

    gate_writes = [
        json.loads(value)
        for key, value in settings.writes
        if key == LOGIN_GATE_KEY and value
    ]
    assert gate_writes
    assert gate_writes[0]["token"] == _token_fingerprint("token")
    assert gate_writes[0]["until"] >= time.time() + 100
    assert settings.data[LOGIN_GATE_KEY] == ""


async def test_start_bot_clears_the_gate_after_success() -> None:
    settings = FakeSettings({LOGIN_GATE_KEY: gate_payload("token", 9999999999.0)})
    bot = FakeBot([])

    await _start_bot(bot, "token", sleep=lambda seconds: None, settings=settings)

    assert settings.data[LOGIN_GATE_KEY] == ""


class FakeHeartbeatStore:
    def __init__(self, heartbeats: list[dict | None]) -> None:
        self.heartbeats = list(heartbeats)

    async def read_heartbeat(self) -> dict | None:
        return self.heartbeats.pop(0) if self.heartbeats else None


async def test_previous_instance_is_waited_out() -> None:
    fresh = {
        "instance_id": "old",
        "updated_at": datetime.now(UTC),
    }
    store = FakeHeartbeatStore([fresh, None])
    health = HealthState(instance_id="new")
    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    await _wait_for_previous_instance(
        store, "new", health, sleep=sleep
    )

    assert sleeps == [5.0]
    assert health.login_state == "waiting_previous"


async def test_stale_or_own_heartbeat_does_not_delay_login() -> None:
    stale = {
        "instance_id": "old",
        "updated_at": datetime.now(UTC) - timedelta(minutes=5),
    }
    own = {"instance_id": "new", "updated_at": datetime.now(UTC)}
    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    await _wait_for_previous_instance(
        FakeHeartbeatStore([stale]), "new", sleep=sleep
    )
    await _wait_for_previous_instance(
        FakeHeartbeatStore([own]), "new", sleep=sleep
    )

    assert sleeps == []


async def test_heartbeat_read_failure_does_not_block_login() -> None:
    class BrokenStore:
        async def read_heartbeat(self) -> dict | None:
            raise RuntimeError("db down")

    await _wait_for_previous_instance(BrokenStore(), "new")
