from discord_bot.llm.model_switcher import ModelSwitcher


class FakeSettings:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    async def update(self, values: dict[str, str]) -> bool:
        self.values.update(values)
        return True


async def test_apply_sets_all_three_settings() -> None:
    settings = FakeSettings()
    switcher = ModelSwitcher(settings)

    ok = await switcher.apply(
        "groq",
        "qwen/qwen3.8-27b",
        "openai/gpt-oss-20b",
    )

    assert ok
    assert settings.values == {
        "provider": "groq",
        "chat_model": "qwen/qwen3.8-27b",
        "utility_model": "openai/gpt-oss-20b",
    }


class PartialFailSettings(FakeSettings):
    async def update(self, values: dict[str, str]) -> bool:
        self.values.update(values)
        return False


async def test_apply_reports_partial_failure() -> None:
    settings = PartialFailSettings()
    switcher = ModelSwitcher(settings)

    ok = await switcher.apply("gemini", "gemini-3.8-flash", "gemini-3.5-flash-lite")

    assert not ok
    assert settings.values["chat_model"] == "gemini-3.8-flash"
