"""Provider registry with built-in presets and user-defined OpenAI-compatible APIs."""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from discord_bot.utils import parse_json_list

logger = logging.getLogger(__name__)

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")


@dataclass(frozen=True, slots=True)
class ProviderSpec:
    name: str
    base_url: str
    api_key_env: str
    default_model: str
    default_utility_model: str
    requires_api_key: bool = True
    supports_tool_choice: bool = True
    free: bool = False

    def api_key(self) -> str | None:
        if not self.api_key_env:
            return None
        return os.getenv(self.api_key_env)


BUILTIN_PROVIDERS: dict[str, ProviderSpec] = {
    "groq": ProviderSpec(
        name="groq",
        base_url="https://api.groq.com/openai/v1",
        api_key_env="GROQ_API_KEY",
        default_model="openai/gpt-oss-120b",
        default_utility_model="openai/gpt-oss-20b",
    ),
    "gemini": ProviderSpec(
        name="gemini",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
        api_key_env="GEMINI_API_KEY",
        default_model="gemini-3.8-flash",
        default_utility_model="gemini-3.5-flash-lite",
    ),
    "openai": ProviderSpec(
        name="openai",
        base_url="https://api.openai.com/v1",
        api_key_env="OPENAI_API_KEY",
        default_model="gpt-4.1-mini",
        default_utility_model="gpt-4.1-nano",
    ),
    "openrouter": ProviderSpec(
        name="openrouter",
        base_url="https://openrouter.ai/api/v1",
        api_key_env="OPENROUTER_API_KEY",
        default_model="nvidia/nemotron-3-ultra-550b-a55b:free",
        default_utility_model="nvidia/nemotron-3-ultra-550b-a55b:free",
    ),
    "ollama": ProviderSpec(
        name="ollama",
        base_url="http://localhost:11434/v1",
        api_key_env="",
        default_model="llama3.1",
        default_utility_model="llama3.1",
        requires_api_key=False,
        free=True,
    ),
}


def is_valid_provider_name(name: str) -> bool:
    return bool(_NAME_RE.fullmatch(name.strip().lower()))


def spec_from_dict(data: Mapping[str, Any]) -> ProviderSpec | None:
    """Build a custom provider from a JSON record. Returns None when invalid."""

    name = str(data.get("name", "")).strip().lower()
    if not _NAME_RE.fullmatch(name):
        logger.warning("Пропускаю провайдера с некорректным именем: %r", name)
        return None
    if name in BUILTIN_PROVIDERS:
        logger.warning("Пропускаю кастомного провайдера %r: имя занято", name)
        return None

    base_url = str(data.get("base_url", "")).strip().rstrip("/")
    if not base_url.startswith(("http://", "https://")):
        logger.warning("Пропускаю провайдера %r: некорректный base_url", name)
        return None

    default_model = str(
        data.get("default_model") or data.get("model") or ""
    ).strip()
    if not default_model:
        logger.warning("Пропускаю провайдера %r: не задана модель", name)
        return None

    default_utility_model = str(
        data.get("default_utility_model") or default_model
    ).strip()
    api_key_env = str(data.get("api_key_env") or "").strip()
    requires_api_key = bool(data.get("requires_api_key", bool(api_key_env)))

    return ProviderSpec(
        name=name,
        base_url=base_url,
        api_key_env=api_key_env,
        default_model=default_model,
        default_utility_model=default_utility_model,
        requires_api_key=requires_api_key,
        supports_tool_choice=bool(data.get("supports_tool_choice", True)),
        free=bool(data.get("free", False)),
    )


def custom_provider_dicts(settings: Mapping[str, str]) -> list[dict[str, Any]]:
    raw = parse_json_list(settings.get("providers_json"), default=[])
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict)]


def build_providers(settings: Mapping[str, str]) -> dict[str, ProviderSpec]:
    """Merge built-in presets with custom providers from bot_settings."""

    providers = dict(BUILTIN_PROVIDERS)
    for item in custom_provider_dicts(settings):
        spec = spec_from_dict(item)
        if spec is not None:
            providers[spec.name] = spec
    return providers
