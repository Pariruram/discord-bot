"""Application services container."""

from __future__ import annotations

from dataclasses import dataclass

from bot.config import Config
from bot.discord_app.chat import ChatService
from bot.llm.client import LLMClient
from bot.storage.cache import SettingsCache
from bot.storage.channel_memory import ChannelMemoryManager
from bot.storage.repository import Repository
from bot.tools.base import ToolRegistry


@dataclass(slots=True)
class Services:
    config: Config
    repo: Repository
    settings: SettingsCache
    memory: ChannelMemoryManager
    llm: LLMClient
    tools: ToolRegistry
    chat: ChatService
