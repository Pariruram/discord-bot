"""Curated catalog of free models shown by ``!models``."""

from __future__ import annotations

import difflib
from collections.abc import Mapping
from dataclasses import dataclass

from bot.llm.providers import ProviderSpec


@dataclass(frozen=True, slots=True)
class ModelOption:
    provider: str
    model: str
    label: str
    description: str
    utility_model: str
    free: bool = True

    @property
    def key(self) -> str:
        return f"{self.provider}:{self.model}"


FREE_MODELS: tuple[ModelOption, ...] = (
    ModelOption(
        provider="gemini",
        model="gemini-3.8-flash",
        label="Gemini 3.8",
        description="1M контекст, tools, free tier",
        utility_model="gemini-3.5-flash-lite",
    ),
    ModelOption(
        provider="gemini",
        model="gemini-3.6-flash",
        label="Gemini 3.6",
        description="предыдущий Flash, 1M контекст",
        utility_model="gemini-3.5-flash-lite",
    ),
    ModelOption(
        provider="gemini",
        model="gemini-3.5-flash-lite",
        label="Gemini 3.5 Lite",
        description="быстрая и дешёвая, free tier",
        utility_model="gemini-3.5-flash-lite",
    ),
    ModelOption(
        provider="gemini",
        model="gemini-3.1-flash-lite",
        label="Gemini 3.1 Lite",
        description="самая лёгкая Gemini, free tier",
        utility_model="gemini-3.1-flash-lite",
    ),
    ModelOption(
        provider="groq",
        model="qwen/qwen3.8-27b",
        label="Groq Qwen 3.8",
        description="coding/agents, tools, 8K TPM",
        utility_model="openai/gpt-oss-20b",
    ),
    ModelOption(
        provider="groq",
        model="openai/gpt-oss-120b",
        label="Groq GPT-OSS 120B",
        description="production, tools, 8K TPM",
        utility_model="openai/gpt-oss-20b",
    ),
    ModelOption(
        provider="groq",
        model="openai/gpt-oss-20b",
        label="Groq GPT-OSS 20B",
        description="самая быстрая, tools, 8K TPM",
        utility_model="openai/gpt-oss-20b",
    ),
    ModelOption(
        provider="openrouter",
        model="nvidia/nemotron-3-ultra-550b-a55b:free",
        label="OR Nemotron Ultra",
        description="1M контекст, tools, free",
        utility_model="nvidia/nemotron-3-ultra-550b-a55b:free",
    ),
    ModelOption(
        provider="openrouter",
        model="nvidia/nemotron-3.5-super-vl:free",
        label="OR Nemotron 3.5",
        description="1M контекст, tools, free",
        utility_model="nvidia/nemotron-3.5-super-vl:free",
    ),
    ModelOption(
        provider="openrouter",
        model="dots-studio/dots-3-note-preview:free",
        label="OR Dots3 Note",
        description="512K контекст, tools, free",
        utility_model="dots-studio/dots-3-note-preview:free",
    ),
    ModelOption(
        provider="openrouter",
        model="google/gemma-4-31b-it:free",
        label="OR Gemma 4 31B",
        description="262K контекст, tools, free",
        utility_model="google/gemma-4-31b-it:free",
    ),
    ModelOption(
        provider="openrouter",
        model="cohere/north-mini-code:free",
        label="OR North Mini",
        description="кодовая модель, 256K, free",
        utility_model="cohere/north-mini-code:free",
    ),
    ModelOption(
        provider="openrouter",
        model="openrouter/free",
        label="OR Free Router",
        description="авто-выбор свободной модели",
        utility_model="openrouter/free",
    ),
)


def provider_is_available(spec: ProviderSpec) -> bool:
    return (not spec.requires_api_key) or bool(spec.api_key())


def available_free_models(
    providers: Mapping[str, ProviderSpec],
) -> list[ModelOption]:
    """Return free catalog models whose provider is configured.

    Only providers with an API key (or no key requirement) are included, and
    duplicate ``provider:model`` pairs are removed.
    """

    result: list[ModelOption] = []
    seen: set[str] = set()

    for option in FREE_MODELS:
        spec = providers.get(option.provider)
        if spec is None or not option.free or not provider_is_available(spec):
            continue
        if option.key in seen:
            continue
        seen.add(option.key)
        result.append(option)

    # Custom / local providers that are not part of the curated catalog.
    # Built-in Ollama is hidden: on Render localhost:11434 is not reachable.
    # A user can add their own local provider via providers_json with "free": true.
    represented = {option.provider for option in FREE_MODELS} | {"ollama"}
    for name, spec in providers.items():
        if name in represented or not provider_is_available(spec):
            continue
        if not (spec.free or not spec.requires_api_key):
            continue
        key = f"{name}:{spec.default_model}"
        if key in seen:
            continue
        seen.add(key)
        result.append(
            ModelOption(
                provider=name,
                model=spec.default_model,
                label=name.title(),
                description="custom free provider",
                utility_model=spec.default_utility_model,
            )
        )

    return result


def _candidate_model_ids(provider: str) -> set[str]:
    candidates = {option.model for option in FREE_MODELS if option.provider == provider}
    return {model for model in candidates if model}


def known_model_ids(provider: str) -> set[str]:
    """Known free model ids for validation and suggestions."""

    return _candidate_model_ids(provider)


def suggest_model(provider: str, model: str) -> str | None:
    """Return a close catalog match for a mistyped model id, if any."""

    candidates = _candidate_model_ids(provider)
    if not candidates or model in candidates:
        return None

    matches = difflib.get_close_matches(model, sorted(candidates), n=1, cutoff=0.6)
    return matches[0] if matches else None


def validate_model(provider: str | None, model: str) -> str | None:
    """Return an actionable validation message, or None when the model is OK."""

    if not provider:
        return None

    for other in ("gemini", "groq", "openrouter"):
        if other != provider and model in known_model_ids(other):
            return (
                f"Модель `{model}` относится к `{other}`, а активный "
                f"провайдер — `{provider}`. Переключите провайдера или "
                f"выберите модель `{provider}`."
            )

    known = known_model_ids(provider)
    if not known or model in known:
        return None

    suggestion = suggest_model(provider, model)
    if suggestion:
        return (
            f"Модель `{model}` не найдена у `{provider}`. "
            f"Возможно, вы имели в виду `{suggestion}`."
        )
    return None
