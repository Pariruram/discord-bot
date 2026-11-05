"""Environment configuration and validation."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)


class ConfigError(RuntimeError):
    """Raised when required environment variables are missing or invalid."""


@dataclass(frozen=True, slots=True)
class Config:
    discord_token: str
    supabase_url: str
    supabase_key: str
    port: int = 7860
    enable_health: bool = True
    log_level: str = "INFO"


def env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off", ""}


def env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("Некорректное значение %s=%r, использую %s", name, raw, default)
        return default


def load_config() -> Config:
    """Load configuration from the environment.

    Only genuinely required variables are validated here. Provider API keys are
    validated lazily when a provider is actually used, so an unused provider
    cannot prevent the bot from starting.
    """

    required = {
        "DISCORD_TOKEN": os.getenv("DISCORD_TOKEN"),
        "SUPABASE_URL": os.getenv("SUPABASE_URL"),
        "SUPABASE_KEY": os.getenv("SUPABASE_KEY"),
    }
    missing = [name for name, value in required.items() if not value or not value.strip()]
    if missing:
        raise ConfigError(
            "Не заданы обязательные переменные окружения: " + ", ".join(missing)
        )

    return Config(
        discord_token=required["DISCORD_TOKEN"].strip(),  # type: ignore[union-attr]
        supabase_url=required["SUPABASE_URL"].strip(),  # type: ignore[union-attr]
        supabase_key=required["SUPABASE_KEY"].strip(),  # type: ignore[union-attr]
        port=env_int("PORT", 7860),
        enable_health=env_bool("ENABLE_HEALTH", True),
        log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
    )
