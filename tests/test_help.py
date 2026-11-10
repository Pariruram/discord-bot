from __future__ import annotations

from discord_bot.discord_app.client import is_help_request
from discord_bot.discord_app.cogs.help import COMMAND_NOTES, help_text


class FakeCommand:
    def __init__(self, name: str, help: str = "") -> None:
        self.name = name
        self.help = help


class FakeCog:
    def __init__(self, *commands: FakeCommand) -> None:
        self._commands = list(commands)

    def get_commands(self) -> list[FakeCommand]:
        return list(self._commands)


class FakeBot:
    """Just enough of commands.Bot for help_text."""

    def __init__(self) -> None:
        self._cogs: dict[str, FakeCog] = {}
        self.commands: list[FakeCommand] = []

    def add_cog(self, name: str, cog: FakeCog, command_names: list[str]) -> None:
        self._cogs[name] = cog
        self.commands.extend(
            FakeCommand(name) for name in command_names
        )

    def get_cog(self, name: str) -> FakeCog | None:
        return self._cogs.get(name)


def make_bot() -> FakeBot:
    bot = FakeBot()
    bot.add_cog(
        "admin",
        FakeCog(
            FakeCommand("owners"),
            FakeCommand("owner"),
            FakeCommand("settings"),
            FakeCommand("set"),
            FakeCommand("system"),
            FakeCommand("model"),
            FakeCommand("provider"),
            FakeCommand("tools"),
        ),
        ["owners", "owner", "settings", "set", "system", "model", "provider", "tools"],
    )
    bot.add_cog(
        "memory",
        FakeCog(FakeCommand("memory"), FakeCommand("forget")),
        ["memory", "forget"],
    )
    bot.add_cog("models", FakeCog(FakeCommand("models")), ["models"])
    return bot


def test_every_registered_command_is_listed() -> None:
    bot = make_bot()
    text = help_text(bot)  # type: ignore[arg-type]

    for command in bot.commands:
        assert f"`!{command.name}`" in text, command.name


def test_unlisted_commands_still_show_up() -> None:
    bot = make_bot()
    bot.commands.append(FakeCommand("brand_new"))

    text = help_text(bot)  # type: ignore[arg-type]

    assert "`!brand_new`" in text
    assert "Прочее" in text


def test_footer_mentions_clean_mode_and_debug() -> None:
    bot = make_bot()

    text = help_text(bot)  # type: ignore[arg-type]

    assert "`/c`" in text
    assert "debug_mode" in text


def test_notes_cover_every_known_command() -> None:
    """The curated map must not drift away from the real commands."""

    bot = make_bot()
    for cog_name, notes in COMMAND_NOTES.items():
        cog = bot.get_cog(cog_name)
        assert cog is not None, cog_name
        for command in cog.get_commands():
            assert command.name in notes, command.name


def test_is_help_request_matches_bare_slash_help_only() -> None:
    assert is_help_request("/help") is True
    assert is_help_request("  /help  ") is True
    assert is_help_request("/help me") is False
    assert is_help_request("!help") is False
    assert is_help_request("") is False
    assert is_help_request(None) is False
