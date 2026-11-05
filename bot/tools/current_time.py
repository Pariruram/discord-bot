"""Current date/time tool (also serves as a minimal pluggable-tool example)."""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from bot.tools.base import Tool, ToolContext


def create_tool() -> Tool:
    async def current_time(context: ToolContext, timezone_name: str = "UTC") -> str:
        try:
            tz = ZoneInfo(timezone_name)
        except (ZoneInfoNotFoundError, ValueError):
            return f"Неизвестный часовой пояс: {timezone_name!r}. Используй IANA-имя, например Europe/Moscow."
        now = datetime.now(UTC).astimezone(tz)
        return now.strftime("%Y-%m-%d %H:%M:%S %Z (%A)")

    return Tool(
        name="current_time",
        description=(
            "Возвращает текущие дату и время. Используй, когда ответ зависит от "
            "сегодняшней даты или времени, либо когда пользователь просит время."
        ),
        parameters={
            "type": "object",
            "properties": {
                "timezone_name": {
                    "type": "string",
                    "description": "IANA-часовой пояс, например Europe/Moscow. По умолчанию UTC.",
                }
            },
            "required": [],
        },
        handler=current_time,
    )
