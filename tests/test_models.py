from bot.llm.models import parse_model_switch, resolve_model_ref
from bot.llm.providers import ProviderSpec

PROVIDERS = {
    "gemini": ProviderSpec(
        name="gemini",
        base_url="https://example.test/gemini",
        api_key_env="GEMINI_API_KEY",
        default_model="gemini-default",
        default_utility_model="gemini-fast",
    ),
    "groq": ProviderSpec(
        name="groq",
        base_url="https://example.test/groq",
        api_key_env="GROQ_API_KEY",
        default_model="groq-default",
        default_utility_model="groq-fast",
    ),
}


def test_legacy_gemini_settings_are_used() -> None:
    ref = resolve_model_ref(
        {
            "provider": "gemini",
            "gemini_model": "gemini-chat",
            "gemini_fast_model": "gemini-utility",
        },
        PROVIDERS,
    )
    assert ref.provider == "gemini"
    assert ref.model == "gemini-chat"
    assert ref.utility_model == "gemini-utility"


def test_legacy_groq_settings_and_defaults() -> None:
    ref = resolve_model_ref(
        {"provider": "groq", "model": "legacy-chat", "fast_model": "legacy-fast"},
        PROVIDERS,
    )
    assert ref.model == "legacy-chat"
    assert ref.utility_model == "legacy-fast"


def test_new_keys_take_priority() -> None:
    ref = resolve_model_ref(
        {
            "provider": "gemini",
            "chat_model": "new-chat",
            "utility_model": "new-utility",
            "gemini_model": "old-chat",
            "gemini_fast_model": "old-utility",
        },
        PROVIDERS,
    )
    assert ref.model == "new-chat"
    assert ref.utility_model == "new-utility"


def test_unknown_provider_falls_back_to_gemini() -> None:
    ref = resolve_model_ref({"provider": "missing"}, PROVIDERS)
    assert ref.provider == "gemini"
    assert ref.model == "gemini-default"


def test_utility_defaults_to_provider_default() -> None:
    ref = resolve_model_ref({"provider": "groq"}, PROVIDERS)
    assert ref.model == "groq-default"
    assert ref.utility_model == "groq-fast"


def test_parse_model_switch() -> None:
    assert parse_model_switch("groq:openai/gpt-oss-120b", PROVIDERS) == (
        "groq",
        "openai/gpt-oss-120b",
    )
    assert parse_model_switch("gemini", PROVIDERS) == ("gemini", "")
    assert parse_model_switch("some/model", PROVIDERS) == (None, "some/model")
    assert parse_model_switch("unknown:model", PROVIDERS) == (None, "unknown:model")
