"""Application entry point."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import time
import uuid
from datetime import UTC, datetime
from typing import Any

import discord

from discord_bot.config import Config, ConfigError, load_config
from discord_bot.discord_app.chat import ChatService
from discord_bot.discord_app.client import create_bot
from discord_bot.discord_app.cogs.admin import AdminCog
from discord_bot.discord_app.cogs.help import HelpCog
from discord_bot.discord_app.cogs.memory import MemoryCog
from discord_bot.discord_app.cogs.models import ModelsCog
from discord_bot.health import HealthState, start_health_server
from discord_bot.llm.client import LLMClient
from discord_bot.llm.errors import retry_after_seconds
from discord_bot.llm.model_switcher import ModelSwitcher
from discord_bot.memory.repository import SupabaseMemoryRepository
from discord_bot.memory.service import MemoryService
from discord_bot.memory.worker import MemoryWorker
from discord_bot.palettes import ensure_palettes
from discord_bot.services import Services
from discord_bot.storage.cache import SettingsCache
from discord_bot.storage.repository import SupabaseRepository
from discord_bot.tools import register_builtin
from discord_bot.tools.base import ToolRegistry, enabled_tool_names
from discord_bot.utils import parse_json_list

logger = logging.getLogger("discord_bot")


def setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        force=True,
    )
    logging.getLogger("discord").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)


async def _close_login_session(bot: Any) -> None:
    """Best-effort cleanup between login attempts.

    discord.py builds a fresh aiohttp session (and TCP connector) on every
    ``static_login`` call and leaves it open when login raises, so each 429
    retry would otherwise leak one session. The connector is also reset,
    because the closed session takes it down with it.
    """

    http = getattr(bot, "http", None)
    if http is None:
        return
    with contextlib.suppress(Exception):
        await http.close()
    http.connector = discord.utils.MISSING


LOGIN_GATE_KEY = "login_blocked_until"
PREVIOUS_INSTANCE_STALE_SECONDS = 180.0
PREVIOUS_INSTANCE_POLL_SECONDS = 5.0


def _token_fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]


async def _read_login_gate(
    settings: Any, token: str, *, now: float | None = None
) -> float | None:
    """Return the persisted Discord 429 deadline for this exact token."""

    try:
        raw = await settings.get(LOGIN_GATE_KEY, "")
    except Exception:  # noqa: BLE001 - a settings failure must not block login
        logger.warning("Не удалось прочитать login-gate", exc_info=True)
        return None
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    if data.get("token") != _token_fingerprint(token):
        # The token was reset: an old block no longer applies.
        return None
    try:
        until = float(data.get("until") or 0)
    except (TypeError, ValueError):
        return None
    current = time.time() if now is None else now
    return until if until > current else None


async def _store_login_gate(
    settings: Any, token: str, until: float
) -> None:
    payload = json.dumps(
        {"until": until, "token": _token_fingerprint(token)}
    )
    try:
        await settings.set(LOGIN_GATE_KEY, payload)
    except Exception:  # noqa: BLE001 - login backoff still works in-process
        logger.warning("Не удалось сохранить login-gate", exc_info=True)


async def _clear_login_gate(settings: Any, token: str) -> None:
    try:
        await settings.set(LOGIN_GATE_KEY, "")
    except Exception:  # noqa: BLE001
        logger.warning("Не удалось очистить login-gate", exc_info=True)


async def _wait_for_login_gate(
    settings: Any,
    token: str,
    health: HealthState | None = None,
    *,
    sleep: Any = asyncio.sleep,
    clock: Any = time.time,
) -> None:
    """Honour a 429 deadline persisted by a previous process/deploy."""

    until = await _read_login_gate(settings, token, now=clock())
    if until is None:
        return
    wait = until - clock()
    if wait <= 0:
        return
    logger.warning(
        "Discord ещё блокирует логин; жду %s сек по сохранённому лимиту",
        int(wait),
    )
    if health is not None:
        health.set_state("waiting_login", retry_at=until)
    await sleep(wait)


def _heartbeat_age(updated_at: Any) -> float | None:
    if not isinstance(updated_at, datetime):
        return None
    if updated_at.tzinfo is None:
        updated_at = updated_at.replace(tzinfo=UTC)
    return (datetime.now(UTC) - updated_at).total_seconds()


async def _wait_for_previous_instance(
    store: Any,
    instance_id: str,
    health: HealthState | None = None,
    *,
    sleep: Any = asyncio.sleep,
    clock: Any = time.monotonic,
) -> None:
    """Wait out a deploy overlap before touching the Discord gateway.

    Render starts the new instance while the old one is still connected. If a
    fresh heartbeat from another process is visible, logging in immediately is
    exactly what triggers a global 429. The old heartbeat goes stale once the
    previous instance is stopped, and only then do we log in.
    """

    logged = False
    last_warning = clock()
    while True:
        try:
            heartbeat = await store.read_heartbeat()
        except Exception:  # noqa: BLE001 - best-effort guard
            logger.warning(
                "Не удалось прочитать heartbeat перед логином", exc_info=True
            )
            return
        if not heartbeat:
            return
        other = str(heartbeat.get("instance_id") or "")
        age = _heartbeat_age(heartbeat.get("updated_at"))
        if not other or other == instance_id or age is None:
            return
        if age > PREVIOUS_INSTANCE_STALE_SECONDS:
            return
        if not logged:
            logger.warning(
                "Виден свежий heartbeat предыдущего инстанса %s; "
                "жду завершения деплоя перед логином",
                other,
            )
            logged = True
        if clock() - last_warning >= 60.0:
            logger.warning(
                "Предыдущий инстанс %s всё ещё пишет heartbeat; продолжаю ждать",
                other,
            )
            last_warning = clock()
        if health is not None:
            health.set_state("waiting_previous")
        await sleep(PREVIOUS_INSTANCE_POLL_SECONDS)


async def _start_bot(
    bot: Any,
    token: str,
    *,
    health: HealthState | None = None,
    sleep: Any = asyncio.sleep,
    settings: Any | None = None,
) -> None:
    """Retry login on a Discord global 429 without a Render crash-loop."""

    attempt = 0
    floors = (300, 900, 1800, 3600)
    while True:
        if health is not None:
            health.set_state("logging_in")
        try:
            await bot.start(token)
            if settings is not None:
                await _clear_login_gate(settings, token)
            return
        except discord.HTTPException as exc:
            if getattr(exc, "status", None) != 429:
                raise
            attempt += 1
            provider_wait = retry_after_seconds(exc) or 0
            floor = floors[min(attempt - 1, len(floors) - 1)]
            wait = max(provider_wait, floor)
            await _close_login_session(bot)
            if settings is not None:
                await _store_login_gate(settings, token, time.time() + wait)
            logger.error(
                "Discord 429 при логине (попытка %s); повтор через %s сек"
                "%s",
                attempt,
                wait,
                f" (Retry-After от Discord: {provider_wait} сек)"
                if provider_wait
                else "",
            )
            if health is not None:
                health.set_state("retrying", retry_at=time.time() + wait)
            await sleep(wait)


def _resvg_status() -> str:
    try:
        import resvg_py  # noqa: F401
    except Exception as exc:  # noqa: BLE001 - diagnostics only
        return f"missing:{type(exc).__name__}"
    return "ok"


async def _migrate_draw_tool(
    settings: SettingsCache, registry: ToolRegistry
) -> None:
    """Enable the new drawing tool for bots that already have a tools list."""

    try:
        if "draw_svg" not in registry.names():
            return
        values = await settings.get_all()
        if values.get("enabled_tools_draw_svg_migrated") == "1":
            return
        raw = values.get("enabled_tools")
        if raw and str(raw).strip():
            parsed = parse_json_list(raw, default=None)
            if isinstance(parsed, list):
                names = [str(item) for item in parsed if isinstance(item, str)]
                if "draw_svg" not in names:
                    names.append("draw_svg")
                    await settings.set(
                        "enabled_tools",
                        json.dumps(names, ensure_ascii=False),
                    )
                    logger.info("Миграция включила draw_svg в enabled_tools")
        await settings.set("enabled_tools_draw_svg_migrated", "1")
    except Exception:  # noqa: BLE001 - a migration must not stop the bot
        logger.exception("Не удалось выполнить миграцию enabled_tools")


async def run_bot(config: Config, health: HealthState | None = None) -> None:
    repo = SupabaseRepository(config.supabase_url, config.supabase_key)
    memory_store = SupabaseMemoryRepository(config.supabase_url, config.supabase_key)
    settings = SettingsCache(repo)
    llm = LLMClient()
    registry = register_builtin(ToolRegistry())
    await _migrate_draw_tool(settings, registry)
    await ensure_palettes(settings)
    if health is not None:
        try:
            values = await settings.get_all()
            health.set_details(
                tools=registry.names(),
                enabled_tools=enabled_tool_names(
                    values.get("enabled_tools"), registry
                ),
                svg_renderer=_resvg_status(),
                svg_model=str(values.get("svg_model") or "provider-default"),
            )
        except Exception:  # noqa: BLE001 - health details are optional
            logger.exception("Не удалось заполнить диагностику /healthz")
    memory = MemoryService(memory_store, settings, llm)
    worker = MemoryWorker(
        memory_store,
        settings,
        llm,
        instance_id=health.instance_id if health is not None else None,
    )
    memory.attach_worker(worker)
    model_switcher = ModelSwitcher(settings)

    await _wait_for_login_gate(settings, config.discord_token, health)
    await _wait_for_previous_instance(memory_store, worker.instance_id, health)

    bot = create_bot()
    bot.health_state = health
    chat = ChatService(bot, settings, memory, llm, registry)
    services = Services(
        config=config,
        repo=repo,
        settings=settings,
        llm=llm,
        tools=registry,
        chat=chat,
        model_switcher=model_switcher,
        memory=memory,
        worker=worker,
    )
    bot.services = services
    await bot.add_cog(AdminCog(bot, services))
    await bot.add_cog(MemoryCog(bot, services))
    await bot.add_cog(ModelsCog(bot, services))
    await bot.add_cog(HelpCog(bot))

    try:
        await _start_bot(
            bot, config.discord_token, health=health, settings=settings
        )
    finally:
        with contextlib.suppress(Exception):
            await bot.close()
        await worker.stop()
        await memory.close()
        await memory_store.close()
        await llm.aclose()
        await repo.close()
        logger.info("Бот остановлен")


def main() -> None:
    try:
        config = load_config()
    except ConfigError as exc:
        logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
        logger.error("%s", exc)
        raise SystemExit(1) from exc

    setup_logging(config.log_level)
    logger.info("Запуск бота")

    health_state = HealthState(instance_id=uuid.uuid4().hex[:12])
    if config.enable_health:
        start_health_server(config.port, health_state)

    try:
        asyncio.run(run_bot(config, health=health_state))
    except KeyboardInterrupt:
        logger.info("Получен Ctrl+C, завершение")
    except Exception:
        logger.exception("Критическая ошибка при запуске")
        raise


if __name__ == "__main__":
    main()
