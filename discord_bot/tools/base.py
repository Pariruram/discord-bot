"""Tool primitives and an isolated tool registry."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from discord_bot.citations import Citation
from discord_bot.utils import parse_json_list, truncate

logger = logging.getLogger(__name__)

DEFAULT_ENABLED_TOOLS = ("web_search",)

ToolHandler = Callable[..., Awaitable[str]]


@dataclass(frozen=True, slots=True)
class ToolContext:
    user_id: str
    channel_id: str
    guild_id: str | None = None
    settings: Mapping[str, str] = field(default_factory=dict)
    citations: list[Citation] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: ToolHandler

    def schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


def enabled_tool_names(raw: str | None, registry: ToolRegistry) -> list[str]:
    """Resolve ``enabled_tools`` from settings, falling back to defaults."""

    available = set(registry.names())
    if raw is None or not str(raw).strip():
        return [name for name in DEFAULT_ENABLED_TOOLS if name in available]

    parsed = parse_json_list(raw, default=None)
    if not isinstance(parsed, list):
        return [name for name in DEFAULT_ENABLED_TOOLS if name in available]

    return [name for name in parsed if isinstance(name, str) and name in available]


class ToolRegistry:
    def __init__(self, *, timeout: float = 20.0, max_result_chars: int = 6000) -> None:
        self._tools: dict[str, Tool] = {}
        self._timeout = timeout
        self._max_result_chars = max_result_chars

    def register(self, tool: Tool) -> None:
        if not tool.name:
            raise ValueError("У инструмента должно быть имя")
        if tool.name in self._tools:
            raise ValueError(f"Инструмент {tool.name!r} уже зарегистрирован")
        self._tools[tool.name] = tool
        logger.info("Зарегистрирован инструмент: %s", tool.name)

    def names(self) -> list[str]:
        return list(self._tools)

    def schemas(self, enabled: Iterable[str] | None = None) -> list[dict[str, Any]]:
        if enabled is None:
            selected = list(self._tools)
        else:
            enabled_set = set(enabled)
            selected = [name for name in self._tools if name in enabled_set]
        return [self._tools[name].schema() for name in selected]

    async def execute(self, name: str, raw_args: str, context: ToolContext) -> str:
        tool = self._tools.get(name)
        if tool is None:
            logger.warning("Модель запросила неизвестный инструмент: %s", name)
            return f"Инструмент {name!r} недоступен."

        try:
            args = json.loads(raw_args or "{}")
        except (TypeError, ValueError):
            return f"Ошибка: аргументы инструмента {name!r} должны быть JSON-объектом."

        if not isinstance(args, dict):
            return f"Ошибка: аргументы инструмента {name!r} должны быть JSON-объектом."

        try:
            result = await asyncio.wait_for(
                tool.handler(context, **args),
                timeout=self._timeout,
            )
        except TimeoutError:
            logger.warning("Инструмент %s не ответил за %.1fs", name, self._timeout)
            return f"Ошибка: инструмент {name!r} не ответил за {self._timeout:.0f} сек."
        except TypeError as exc:
            logger.warning("Неверные аргументы инструмента %s: %s", name, exc)
            return f"Ошибка аргументов инструмента {name!r}: {exc}"
        except Exception as exc:  # noqa: BLE001 - tool failures must not break the chat
            logger.exception("Ошибка выполнения инструмента %s", name)
            return f"Ошибка выполнения инструмента {name!r}: {exc}"

        if not isinstance(result, str):
            result = str(result)
        return truncate(result, self._max_result_chars)
