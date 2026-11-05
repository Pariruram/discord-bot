"""Tool discovery.

To add a tool, create a module in this package that exposes ``create_tool()``
returning a :class:`bot.tools.base.Tool`. It is picked up automatically.
"""

from __future__ import annotations

import importlib
import logging
import pkgutil

from discord_bot.tools.base import ToolRegistry

logger = logging.getLogger(__name__)


def register_builtin(registry: ToolRegistry) -> ToolRegistry:
    for module_info in pkgutil.iter_modules(__path__):
        if module_info.name.startswith("_") or module_info.name == "base":
            continue
        module_name = f"{__name__}.{module_info.name}"
        try:
            module = importlib.import_module(module_name)
            factory = getattr(module, "create_tool", None)
            if callable(factory):
                registry.register(factory())
        except Exception:  # noqa: BLE001 - one broken tool must not stop the bot
            logger.exception("Не удалось загрузить инструмент из %s", module_name)
    return registry
