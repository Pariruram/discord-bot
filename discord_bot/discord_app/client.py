"""Discord client wiring."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

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


def create_bot() -> DiscordBot:
    bot = DiscordBot()

    @bot.event
    async def on_ready() -> None:
        logger.info(
            "Бот %s запущен; серверов: %s",
            bot.user,
            len(bot.guilds),
        )

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
