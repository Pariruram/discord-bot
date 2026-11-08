from __future__ import annotations

from types import SimpleNamespace

import discord

from discord_bot.discord_app.addressing import classify_message, is_dm_channel


class FakeBotUser:
    id = 999

    def __init__(self, mentioned: bool = False) -> None:
        self._mentioned = mentioned

    def mentioned_in(self, message: object) -> bool:
        return self._mentioned


def dm_message() -> SimpleNamespace:
    return SimpleNamespace(
        channel=SimpleNamespace(type=discord.ChannelType.private, id=1),
        reference=None,
    )


def guild_message(
    *,
    mentioned: bool = False,
    reply_author_id: int | None = None,
) -> SimpleNamespace:
    reference = None
    if reply_author_id is not None:
        reference = SimpleNamespace(
            message_id=55,
            resolved=SimpleNamespace(
                id=55,
                author=SimpleNamespace(id=reply_author_id),
            ),
        )
    return SimpleNamespace(
        channel=SimpleNamespace(type=discord.ChannelType.text, id=2),
        reference=reference,
        mentioned=mentioned,
    )


def test_dm_is_always_addressed_to_bot() -> None:
    result = classify_message(dm_message(), FakeBotUser())

    assert result.addressed_to == "bot"


def test_mention_is_addressed_to_bot() -> None:
    result = classify_message(guild_message(), FakeBotUser(mentioned=True))

    assert result.addressed_to == "bot"


def test_reply_to_bot_is_addressed_to_bot() -> None:
    result = classify_message(
        guild_message(reply_author_id=999),
        FakeBotUser(),
    )

    assert result.addressed_to == "bot"
    assert result.reply_to_id == "55"


def test_reply_to_human_is_not_addressed_to_bot() -> None:
    result = classify_message(
        guild_message(reply_author_id=123),
        FakeBotUser(),
    )

    assert result.addressed_to == "human"
    assert result.reply_to_author_id == "123"


def test_plain_channel_message_is_ambient() -> None:
    result = classify_message(guild_message(), FakeBotUser())

    assert result.addressed_to == "ambient"
    assert result.reply_to_id is None


def test_dm_channel_detection() -> None:
    assert is_dm_channel(SimpleNamespace(type=discord.ChannelType.private))
    assert not is_dm_channel(SimpleNamespace(type=discord.ChannelType.text))
