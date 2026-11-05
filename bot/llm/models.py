"""Model references and backward-compatible settings resolution."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass

from bot.llm.providers import ProviderSpec

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ModelRef:
    provider: str
    model: str
    utility_model: str

    @property
    def label(self) -> str:
        return f"{self.provider}:{self.model}"


def _first(*values: object) -> str:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _legacy_chat_model(settings: Mapping[str, str], provider: str) -> str:
    if provider == "gemini":
        return _first(settings.get("gemini_model"))
    if provider == "groq":
        return _first(settings.get("groq_model"), settings.get("model"))
    return _first(settings.get("model"))


def _legacy_utility_model(settings: Mapping[str, str], provider: str) -> str:
    if provider == "gemini":
        return _first(settings.get("gemini_fast_model"))
    if provider == "groq":
        return _first(settings.get("groq_fast_model"), settings.get("fast_model"))
    return _first(settings.get("fast_model"))


def resolve_model_ref(
    settings: Mapping[str, str],
    providers: Mapping[str, ProviderSpec],
) -> ModelRef:
    """Resolve the active provider and models, honouring the old settings keys."""

    if not providers:
        raise ValueError("Не настроен ни один LLM-провайдер")

    active = _first(settings.get("provider"), "gemini").lower()
    if active not in providers:
        logger.warning("Неизвестный провайдер %r, использую gemini", active)
        active = "gemini" if "gemini" in providers else next(iter(providers))

    spec = providers[active]

    chat_model = _first(
        settings.get("chat_model"),
        _legacy_chat_model(settings, active),
        spec.default_model,
    )
    utility_model = _first(
        settings.get("utility_model"),
        _legacy_utility_model(settings, active),
        spec.default_utility_model,
        chat_model,
    )

    return ModelRef(provider=active, model=chat_model, utility_model=utility_model)


def parse_model_switch(
    value: str,
    providers: Mapping[str, ProviderSpec],
) -> tuple[str | None, str]:
    """Parse ``provider:model``, bare ``provider`` or bare ``model``.

    Returns ``(provider_or_none, model)``. Splitting happens on the first colon
    only, because model ids may contain slashes.
    """

    raw = value.strip()
    if not raw:
        return None, ""

    if ":" in raw:
        prefix, rest = raw.split(":", 1)
        prefix = prefix.strip().lower()
        if prefix in providers:
            return prefix, rest.strip()

    lowered = raw.lower()
    if lowered in providers:
        return lowered, ""

    return None, raw
