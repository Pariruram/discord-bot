"""Global model switching used by ``!models`` and owner commands."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bot.storage.cache import SettingsCache

logger = logging.getLogger(__name__)


class ModelSwitcher:
    def __init__(self, settings: SettingsCache) -> None:
        self._settings = settings
        self._lock = asyncio.Lock()

    async def apply(
        self,
        provider: str,
        model: str,
        utility_model: str,
    ) -> bool:
        """Atomically update the global provider/chat/utility settings."""

        async with self._lock:
            ok = await self._settings.update(
                {
                    "provider": provider,
                    "chat_model": model,
                    "utility_model": utility_model,
                }
            )

        if not ok:
            logger.warning(
                "Не все настройки модели сохранились: provider=%s chat=%s utility=%s",
                provider,
                model,
                utility_model,
            )
        return ok
