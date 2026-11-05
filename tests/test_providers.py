from discord_bot.llm.providers import BUILTIN_PROVIDERS, build_providers, spec_from_dict


def test_custom_provider_is_added() -> None:
    providers = build_providers(
        {
            "providers_json": (
                '[{"name":"local","base_url":"http://localhost:11434/v1",'
                '"api_key_env":"","default_model":"llama3.1"}]'
            )
        }
    )
    assert "local" in providers
    assert providers["local"].default_model == "llama3.1"
    assert providers["local"].requires_api_key is False


def test_invalid_custom_providers_are_skipped() -> None:
    providers = build_providers(
        {
            "providers_json": (
                '[{"name":"bad name","base_url":"http://x","default_model":"m"},'
                '{"name":"badurl","base_url":"ftp://x","default_model":"m"},'
                '{"name":"nomodel","base_url":"http://x"}]'
            )
        }
    )
    assert "bad name" not in providers
    assert "badurl" not in providers
    assert "nomodel" not in providers


def test_custom_provider_cannot_override_builtin() -> None:
    providers = build_providers(
        {
            "providers_json": (
                '[{"name":"groq","base_url":"http://evil","default_model":"m"}]'
            )
        }
    )
    assert providers["groq"] == BUILTIN_PROVIDERS["groq"]


def test_spec_from_dict_requires_valid_name_and_url() -> None:
    assert spec_from_dict({"name": "ok", "base_url": "https://x", "default_model": "m"})
    assert not spec_from_dict({"name": "OK NAME", "base_url": "https://x", "default_model": "m"})
    assert not spec_from_dict({"name": "ok", "base_url": "x", "default_model": "m"})
