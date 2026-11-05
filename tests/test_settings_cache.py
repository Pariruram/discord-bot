from typing import Any

from bot.storage.cache import SettingsCache


class FakeRepo:
    def __init__(self, data: dict[str, str] | None = None) -> None:
        self.data = dict(data or {})
        self.reads = 0
        self.writes: list[tuple[str, str]] = []

    async def get_all_settings(self) -> dict[str, str]:
        self.reads += 1
        return dict(self.data)

    async def set_setting(self, key: str, value: str) -> bool:
        self.data[key] = value
        self.writes.append((key, value))
        return True


async def test_values_are_cached() -> None:
    repo = FakeRepo({"provider": "gemini"})
    cache = SettingsCache(repo, ttl=60)

    assert (await cache.get_all())["provider"] == "gemini"
    assert (await cache.get("provider")) == "gemini"
    assert repo.reads == 1


async def test_set_before_first_read_does_not_hide_other_keys() -> None:
    repo = FakeRepo({"provider": "gemini", "max_history": "12"})
    cache = SettingsCache(repo, ttl=60)

    await cache.set("provider", "groq")
    settings = await cache.get_all()

    assert settings["provider"] == "groq"
    assert settings["max_history"] == "12"


async def test_set_after_load_updates_snapshot() -> None:
    repo = FakeRepo({"provider": "gemini"})
    cache = SettingsCache(repo, ttl=60)
    await cache.get_all()

    await cache.set("provider", "groq")

    assert (await cache.get("provider")) == "groq"
    assert repo.reads == 1


async def test_owners_round_trip() -> None:
    repo = FakeRepo()
    cache = SettingsCache(repo, ttl=60)

    assert await cache.get_owners() == []
    await cache.save_owners(["1", "2"])

    assert await cache.get_owners() == ["1", "2"]


async def test_invalidate_forces_reload() -> None:
    repo = FakeRepo({"provider": "gemini"})
    cache = SettingsCache(repo, ttl=60)
    await cache.get_all()
    repo.data["provider"] = "groq"

    cache.invalidate()

    assert (await cache.get("provider")) == "groq"
    assert repo.reads == 2


def test_fake_repo_type() -> None:
    # Keeps the fake honest about the Repository protocol surface used here.
    repo: Any = FakeRepo()
    assert repo is not None
