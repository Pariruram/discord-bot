from __future__ import annotations

import discord
import pytest

from discord_bot.main import _close_login_session, _start_bot


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
