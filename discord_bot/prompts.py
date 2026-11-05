"""System prompt construction."""

from __future__ import annotations

DEFAULT_SYSTEM_PROMPT = "Ты — полезный ассистент."


def build_system_prompt(
    global_prompt: str,
    preferences: dict[str, object] | None = None,
    channel_context: str = "",
) -> str:
    parts = [global_prompt or DEFAULT_SYSTEM_PROMPT]

    if preferences:
        lines = [f"- {key}: {value}" for key, value in preferences.items()]
        parts.append("Предпочтения:\n" + "\n".join(lines))

    if channel_context:
        parts.append(channel_context)

    return "\n\n".join(parts)
