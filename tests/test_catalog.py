from discord_bot.llm.catalog import (
    available_free_models,
    known_model_ids,
    suggest_model,
    validate_model,
)
from discord_bot.llm.providers import ProviderSpec


def spec(
    name: str,
    key_env: str,
    *,
    requires_api_key: bool = True,
    free: bool = False,
    default_model: str = "model",
    default_utility_model: str = "utility",
) -> ProviderSpec:
    return ProviderSpec(
        name=name,
        base_url=f"https://{name}.example/v1",
        api_key_env=key_env,
        default_model=default_model,
        default_utility_model=default_utility_model,
        requires_api_key=requires_api_key,
        free=free,
    )


def test_only_providers_with_keys(monkeypatch) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("GROQ_API_KEY", "test")

    options = available_free_models(
        {
            "gemini": spec("gemini", "GEMINI_API_KEY"),
            "groq": spec("groq", "GROQ_API_KEY"),
        }
    )

    assert options
    assert {option.provider for option in options} == {"groq"}


def test_no_duplicate_models(monkeypatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "test")
    monkeypatch.setenv("GROQ_API_KEY", "test")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test")

    options = available_free_models(
        {
            "gemini": spec("gemini", "GEMINI_API_KEY"),
            "groq": spec("groq", "GROQ_API_KEY"),
            "openrouter": spec("openrouter", "OPENROUTER_API_KEY"),
        }
    )

    keys = [option.key for option in options]
    assert len(keys) == len(set(keys))


def test_no_key_required_provider_is_added() -> None:
    options = available_free_models(
        {
            "local": spec(
                "local",
                "",
                requires_api_key=False,
                free=True,
                default_model="local-model",
                default_utility_model="local-model",
            )
        }
    )

    assert [option.key for option in options] == ["local:local-model"]


def test_keyed_non_free_custom_provider_is_hidden(monkeypatch) -> None:
    monkeypatch.setenv("PAID_API_KEY", "test")
    options = available_free_models(
        {
            "paid": spec(
                "paid",
                "PAID_API_KEY",
                requires_api_key=True,
                free=False,
                default_model="paid-model",
            )
        }
    )
    assert options == []


def test_suggest_model_for_typo() -> None:
    assert suggest_model("gemini", "gemini-3.5-lite") == "gemini-3.5-flash-lite"


def test_known_model_ids_contains_catalog() -> None:
    assert "gemini-3.8-flash" in known_model_ids("gemini")
    assert "qwen/qwen3.8-27b" in known_model_ids("groq")


def test_validate_model_suggests_close_match() -> None:
    message = validate_model("gemini", "gemini-3.5-lite")
    assert message is not None
    assert "gemini-3.5-flash-lite" in message


def test_validate_model_accepts_known_model() -> None:
    assert validate_model("gemini", "gemini-3.5-flash-lite") is None


def test_validate_model_rejects_other_provider_model() -> None:
    message = validate_model("gemini", "qwen/qwen3.8-27b")
    assert message is not None
    assert "groq" in message
