"""Application services container."""

from __future__ import annotations

from dataclasses import dataclass

from discord_bot.config import Config
from discord_bot.discord_app.chat import ChatService
from discord_bot.llm.client import LLMClient
from discord_bot.llm.model_switcher import ModelSwitcher
from discord_bot.memory.service import MemoryService
from discord_bot.memory.worker import MemoryWorker
from discord_bot.storage.cache import SettingsCache
from discord_bot.storage.repository import Repository
from discord_bot.tools.base import ToolRegistry


@dataclass(slots=True)
class Services:
    config: Config
    repo: Repository
    settings: SettingsCache
    llm: LLMClient
    tools: ToolRegistry
    chat: ChatService
    model_switcher: ModelSwitcher
    memory: MemoryService
    worker: MemoryWorker
