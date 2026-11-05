"""Application entry point."""

from __future__ import annotations

import asyncio
import contextlib
import logging

from bot.config import Config, ConfigError, load_config
from bot.discord_app.bot import create_bot
from bot.discord_app.chat import ChatService
from bot.discord_app.cogs.admin import AdminCog
from bot.discord_app.cogs.models import ModelsCog
from bot.health import start_health_server
from bot.llm.client import LLMClient
from bot.llm.model_switcher import ModelSwitcher
from bot.services import Services
from bot.storage.cache import SettingsCache
from bot.storage.channel_memory import ChannelMemoryManager
from bot.storage.repository import SupabaseRepository
from bot.tools import register_builtin
from bot.tools.base import ToolRegistry

logger = logging.getLogger("bot")


def setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        force=True,
    )
    logging.getLogger("discord").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)


async def run_bot(config: Config) -> None:
    repo = SupabaseRepository(config.supabase_url, config.supabase_key)
    settings = SettingsCache(repo)
    llm = LLMClient()
    registry = register_builtin(ToolRegistry())
    memory = ChannelMemoryManager(repo)
    model_switcher = ModelSwitcher(settings)

    bot = create_bot()
    chat = ChatService(bot, repo, settings, memory, llm, registry)
    services = Services(
        config=config,
        repo=repo,
        settings=settings,
        memory=memory,
        llm=llm,
        tools=registry,
        chat=chat,
        model_switcher=model_switcher,
    )
    bot.services = services
    await bot.add_cog(AdminCog(bot, services))
    await bot.add_cog(ModelsCog(bot, services))

    flusher = asyncio.create_task(memory.run_flusher(), name="channel-memory-flusher")

    try:
        await bot.start(config.discord_token)
    finally:
        flusher.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await flusher
        await memory.flush_all()
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
