"""The single place where every command is listed for the user.

The listing is generated from the registered commands, so a new command
cannot silently disappear from ``!help``.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING

from discord.ext import commands

from discord_bot.utils import split_message

if TYPE_CHECKING:
    from discord_bot.discord_app.client import DiscordBot

# One curated line per command; anything not listed here still shows up
# automatically under its cog.
COMMAND_NOTES = {
    "admin": {
        "settings": "все настройки бота",
        "set": "смена настройки: `!set <ключ> <значение>`",
        "system": "статус системы и провайдеров",
        "model": "аварийная смена модели: `!model <имя>`",
        "provider": "провайдеры: `!provider add|remove|list …`",
        "tools": "инструменты и их включение",
        "owners": "список владельцев",
        "owner": "владельцы: `!owner add|remove <id>`",
    },
    "memory": {
        "memory": "память: `!memory [on|off]`, иначе — статистика",
        "forget": "удалить память: `!forget <номер|all>`",
    },
    "models": {
        "models": "доступные модели и смена текущей",
    },
}

SECTION_TITLES = {
    "admin": "Отладка и настройки (только владелец, в личных сообщениях)",
    "memory": "Память (в личных сообщениях)",
    "models": "Модели",
}

FOOTER = (
    "Фоновые функции:\n"
    "`/c` или `/clean` в конце сообщения — фактический ответ без личности\n"
    "`/svg` в конце сообщения — приложить исходник SVG к рисунку\n"
    "Пришли `.svg` и попроси «улучши» — верну PNG и улучшенный исходник.\n"
    "`!set debug_mode true` — техническая строка после каждого ответа\n"
    "Попроси нарисовать схему или иллюстрацию — пришлю PNG (SVG — по запросу).\n"
    "Память автоматически хранит договорённости, факты и резюме диалогов."
)


def help_text(bot: DiscordBot) -> str:
    """Render the command map; anything unlisted is still included."""

    covered: set[str] = set()
    sections: list[str] = []
    for cog_name, notes in COMMAND_NOTES.items():
        cog = bot.get_cog(cog_name)
        if cog is None:
            continue
        lines: list[str] = []
        for command in _sorted_cog_commands(cog):
            note = notes.get(command.name)
            covered.add(command.name)
            if note is None:
                note = command.help or ""
            heading = f"`!{command.name}`"
            lines.append(f"{heading} — {note}" if note else heading)
        if lines:
            sections.append(f"**{SECTION_TITLES[cog_name]}**\n" + "\n".join(lines))

    leftover: list[str] = []
    for command in _sorted_names(bot.commands):
        if command in covered:
            continue
        leftover.append(f"`!{command}`")
    if leftover:
        sections.append("**Прочее**\n" + ", ".join(leftover))

    return "\n\n".join([*sections, FOOTER])


def _sorted_cog_commands(cog: commands.Cog) -> Iterable[commands.Command]:
    yield from sorted(cog.get_commands(), key=lambda cmd: cmd.name)


def _sorted_names(commands_: Iterable[commands.Command]) -> list[str]:
    return sorted(command.name for command in commands_)


class HelpCog(commands.Cog, name="help"):
    """``!help`` everywhere; the same text answers a bare ``/help``."""

    def __init__(self, bot: DiscordBot) -> None:
        self.bot = bot

    @commands.command(name="help")
    async def help_cmd(self, ctx: commands.Context) -> None:
        text = help_text(self.bot)
        for chunk in split_message(text, max_len=1900):
            await ctx.send(chunk)
