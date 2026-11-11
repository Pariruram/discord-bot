from __future__ import annotations

import json
from typing import Any

from discord_bot.health import HealthState
from discord_bot.main import _migrate_draw_tool


def test_health_details_appear_in_the_snapshot() -> None:
    state = HealthState(instance_id="abc")
    state.set_state("connected")
    state.set_details(tools=["draw_svg"], svg_renderer="ok")

    payload = state.snapshot()

    assert payload["instance_id"] == "abc"
    assert payload["login_state"] == "connected"
    assert payload["details"] == {"tools": ["draw_svg"], "svg_renderer": "ok"}


class FakeSettings:
    def __init__(self, data: dict[str, str]) -> None:
        self.data = dict(data)
        self.writes: list[tuple[str, str]] = []

    async def get_all(self) -> dict[str, str]:
        return dict(self.data)

    async def set(self, key: str, value: str) -> None:
        self.data[key] = value
        self.writes.append((key, value))


class FakeRegistry:
    def __init__(self, names: list[str]) -> None:
        self._names = names

    def names(self) -> list[str]:
        return list(self._names)


async def test_migration_adds_draw_svg_to_an_existing_list() -> None:
    settings = FakeSettings({"enabled_tools": '["web_search"]'})

    await _migrate_draw_tool(settings, FakeRegistry(["web_search", "draw_svg"]))

    assert json.loads(settings.data["enabled_tools"]) == [
        "web_search",
        "draw_svg",
    ]
    assert settings.data["enabled_tools_draw_svg_migrated"] == "1"


async def test_migration_does_not_re_add_after_the_marker() -> None:
    settings = FakeSettings(
        {
            "enabled_tools": '["web_search"]',
            "enabled_tools_draw_svg_migrated": "1",
        }
    )

    await _migrate_draw_tool(settings, FakeRegistry(["web_search", "draw_svg"]))

    assert json.loads(settings.data["enabled_tools"]) == ["web_search"]
    assert settings.writes == []


async def test_migration_is_silent_without_an_explicit_list() -> None:
    settings = FakeSettings({})

    await _migrate_draw_tool(settings, FakeRegistry(["web_search", "draw_svg"]))

    assert "enabled_tools" not in settings.data
    assert settings.data["enabled_tools_draw_svg_migrated"] == "1"


async def test_migration_waits_for_the_tool_to_register() -> None:
    settings = FakeSettings({"enabled_tools": '["web_search"]'})

    await _migrate_draw_tool(settings, FakeRegistry(["web_search"]))

    assert settings.writes == []
    assert json.loads(settings.data["enabled_tools"]) == ["web_search"]


async def test_migration_handles_broken_settings() -> None:
    class BrokenSettings:
        async def get_all(self) -> dict[str, Any]:
            raise RuntimeError("db down")

    await _migrate_draw_tool(
        BrokenSettings(), FakeRegistry(["web_search", "draw_svg"])
    )
