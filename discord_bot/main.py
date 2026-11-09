"""Application entry point."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

import discord

from discord_bot.config import Config, ConfigError, load_config
from discord_bot.discord_app.chat import ChatService
from discord_bot.discord_app.client import create_bot
from discord_bot.discord_app.cogs.admin import AdminCog
from discord_bot.discord_app.cogs.memory import MemoryCog
from discord_bot.discord_app.cogs.models import ModelsCog
from discord_bot.health import start_health_server
from discord_bot.llm.client import LLMClient
from discord_bot.llm.errors import retry_after_seconds
from discord_bot.llm.model_switcher import ModelSwitcher
from discord_bot.memory.repository import SupabaseMemoryRepository
from discord_bot.memory.service import MemoryService
from discord_bot.memory.worker import MemoryWorker
from discord_bot.services import Services
from discord_bot.storage.cache import SettingsCache
from discord_bot.storage.repository import SupabaseRepository
from discord_bot.tools import register_builtin
from discord_bot.tools.base import ToolRegistry

logger = logging.getLogger("discord_bot")


def setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        force=True,
    )
    logging.getLogger("discord").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)


async def _start_bot(bot: Any, token: str, *, sleep: Any = asyncio.sleep) -> None:
    """Retry login on a Discord global 429 without a Render crash-loop."""

    while True:
        try:
            await bot.start(token)
            return
        except discord.HTTPException as exc:
            if getattr(exc, "status", None) != 429:
                raise
            wait = max(retry_after_seconds(exc) or 900, 300)
            logger.error(
                "Discord 429 при логине; повтор через %s сек", wait
            )
            await sleep(wait)


async def run_bot(config: Config) -> None:
    repo = SupabaseRepository(config.supabase_url, config.supabase_key)
    memory_store = SupabaseMemoryRepository(config.supabase_url, config.supabase_key)
    settings = SettingsCache(repo)
    llm = LLMClient()
    registry = register_builtin(ToolRegistry())
    memory = MemoryService(memory_store, settings, llm)
    worker = MemoryWorker(memory_store, settings, llm)
    memory.attach_worker(worker)
    model_switcher = ModelSwitcher(settings)

    bot = create_bot()
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

    try:
        await _start_bot(bot, config.discord_token)
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

    if config.enable_health:
        start_health_server(config.port)

    try:
        asyncio.run(run_bot(config))
    except KeyboardInterrupt:
        logger.info("Получен Ctrl+C, завершение")
    except Exception:
        logger.exception("Критическая ошибка при запуске")
        raise


if __name__ == "__main__":
    main()
