from __future__ import annotations

import discord
import pytest

from discord_bot.main import _start_bot


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


class FakeBot:
    def __init__(self, failures: list[Exception]) -> None:
        self.failures = list(failures)
        self.calls = 0

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
