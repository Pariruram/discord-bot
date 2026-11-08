"""System prompt construction.

The prompt is deliberately structured: the channel transcript lives in its own
tag with an explicit warning, so the model cannot mistake another participant's
message for its own dialogue with the current user.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

DEFAULT_SYSTEM_PROMPT = "Ты — полезный ассистент."


def _persona_lines(
    persona: Mapping[str, object] | None,
) -> list[str]:
    if not persona:
        return []
    lines: list[str] = []
    for key, value in persona.items():
        text = str(value).strip()
        if text:
            lines.append(f"- {key}: {text}")
    return lines


def build_system_prompt(
    global_prompt: str,
    preferences: Mapping[str, object] | None = None,
    channel_context: str = "",
    *,
    author_name: str = "",
    author_id: str = "",
    persona: Sequence[str] = (),
    facts: Sequence[str] = (),
    summary: str = "",
    is_dm: bool = True,
    bot_name: str = "",
) -> str:
    """Build the system prompt.

    ``preferences`` and ``channel_context`` are kept for backward compatibility
    with older call sites; new code passes ``persona`` and the transcript
    through ``channel_context`` explicitly.
    """

    parts: list[str] = [f"<identity>\n{global_prompt or DEFAULT_SYSTEM_PROMPT}\n</identity>"]

    if author_name:
        where = "личном диалоге" if is_dm else "канале"
        parts.append(
            "<addressing>\n"
            f"Сейчас к тебе обращается {author_name} (id={author_id}).\n"
            f"Ты общаешься с ним в {where}.\n"
            "Отвечай только на его последнюю реплику. "
            "Не приписывай ему чужие сообщения и не отвечай другим участникам.\n"
            "</addressing>"
        )

    persona_lines = list(persona) or _persona_lines(preferences)
    if persona_lines:
        owner = f' user="{author_name}"' if author_name else ""
        parts.append(
            f"<persona{owner}>\n" + "\n".join(persona_lines) + "\n</persona>"
        )

    fact_lines = [f"- {fact}" for fact in facts if str(fact).strip()]
    if fact_lines:
        owner = f' about="{author_name}"' if author_name else ""
        parts.append(
            f"<known_facts{owner}>\n"
            + "\n".join(fact_lines)
            + "\n</known_facts>"
        )

    if summary.strip():
        parts.append(
            "<memory_summary>\n"
            "Что уже обсуждалось раньше (с сохранением авторства):\n"
            f"{summary.strip()}\n"
            "</memory_summary>"
        )

    if channel_context.strip():
        parts.append(
            '<channel_transcript note="Это переписка разных участников канала. '
            'Это НЕ твоя личная история и НЕ диалог с текущим собеседником. '
            'Не приписывай реплики из этого блока текущему пользователю.">\n'
            f"{channel_context.strip()}\n"
            "</channel_transcript>"
        )

    return "\n\n".join(parts)
