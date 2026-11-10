"""Discord client wiring."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import discord
from discord.ext import commands

if TYPE_CHECKING:
    from discord_bot.services import Services

logger = logging.getLogger(__name__)

COMMAND_PREFIX = "!"


class DiscordBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix=COMMAND_PREFIX, intents=intents)
        self.services: Services | None = None
        self.health_state: Any = None


def create_bot() -> DiscordBot:
    bot = DiscordBot()

    @bot.event
    async def on_ready() -> None:
        if bot.health_state is not None:
            bot.health_state.set_state("connected", retry_at=None)
        logger.info(
            "Бот %s запущен; серверов: %s",
            bot.user,
            len(bot.guilds),
        )
        if bot.services is not None:
            try:
                await bot.services.memory.start_background(bot)
            except Exception:  # noqa: BLE001 - never break the gateway connection
                logger.exception("Не удалось запустить фоновые задачи памяти")

    @bot.event
    async def on_message(message: discord.Message) -> None:
        if message.author.bot:
            return

        await bot.process_commands(message)

        if message.content.startswith(COMMAND_PREFIX):
            return

        if bot.services is None:
            logger.error("Сервисы ещё не инициализированы, сообщение пропущено")
            return

        await bot.services.chat.handle(message)

    @bot.event
    async def on_message_edit(before: discord.Message, after: discord.Message) -> None:
        if after.author.bot or bot.services is None:
            return
        if (before.content or "") == (after.content or ""):
            return
        await bot.services.memory.update_message(after)

    @bot.event
    async def on_message_delete(message: discord.Message) -> None:
        if message.author.bot or bot.services is None:
            return
        await bot.services.memory.forget_message(message.id)

    @bot.event
    async def on_bulk_message_delete(messages: list[discord.Message]) -> None:
        if bot.services is None:
            return
        for message in messages:
            if message.author.bot:
                continue
            await bot.services.memory.forget_message(message.id)

    @bot.event
    async def on_command_error(ctx: commands.Context, error: Exception) -> None:
        if isinstance(error, commands.CommandNotFound):
            return

        original = getattr(error, "original", error)
        logger.error("Ошибка команды %s: %s", ctx.command, original, exc_info=original)

        try:
            await ctx.send("Ошибка выполнения команды. Подробности в логах.")
        except discord.HTTPException:
            logger.debug("Не удалось сообщить об ошибке команды", exc_info=True)

    return bot
