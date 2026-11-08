"""Classify who a Discord message is addressed to.

The reply graph is the reliable source of truth: a page-long transcript is not
enough to tell whether the bot should answer, and guessing from text is exactly
what caused one user's topic to leak into another user's answer.
"""

from __future__ import annotations

from dataclasses import dataclass

import discord


@dataclass(frozen=True, slots=True)
class Addressing:
    addressed_to: str  # "bot" | "human" | "ambient"
    reply_to_id: str | None = None
    reply_to_author_id: str | None = None


def is_dm_channel(channel: object) -> bool:
    if isinstance(channel, discord.DMChannel):
        return True
    return getattr(channel, "type", None) is discord.ChannelType.private


def is_thread_channel(channel: object) -> bool:
    if isinstance(channel, discord.Thread):
        return True
    return getattr(channel, "type", None) in {
        discord.ChannelType.public_thread,
        discord.ChannelType.private_thread,
        discord.ChannelType.news_thread,
    }


def _reply_target(message: object) -> tuple[str | None, str | None]:
    reference = getattr(message, "reference", None)
    if reference is None:
        return None, None

    resolved = getattr(reference, "resolved", None)
    if resolved is not None:
        message_id = getattr(resolved, "id", None) or getattr(
            reference, "message_id", None
        )
        author = getattr(resolved, "author", None)
        author_id = getattr(author, "id", None)
        return (
            str(message_id) if message_id is not None else None,
            str(author_id) if author_id is not None else None,
        )

    message_id = getattr(reference, "message_id", None)
    return (str(message_id) if message_id is not None else None), None


def classify_message(
    message: object,
    bot_user: object | None,
) -> Addressing:
    reply_to_id, reply_author_id = _reply_target(message)

    channel = getattr(message, "channel", None)
    if is_dm_channel(channel):
        return Addressing("bot", reply_to_id, reply_author_id)

    bot_id = str(getattr(bot_user, "id", "")) if bot_user is not None else ""
    if bot_user is not None:
        mentioned_in = getattr(bot_user, "mentioned_in", None)
        if callable(mentioned_in) and mentioned_in(message):
            return Addressing("bot", reply_to_id, reply_author_id)

    if reply_author_id and bot_id and reply_author_id == bot_id:
        return Addressing("bot", reply_to_id, reply_author_id)

    if reply_author_id:
        return Addressing("human", reply_to_id, reply_author_id)

    return Addressing("ambient", reply_to_id, reply_author_id)
