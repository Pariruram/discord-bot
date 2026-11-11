"""System prompt construction.

The prompt is deliberately structured: the channel transcript lives in its own
tag with an explicit warning, so the model cannot mistake another participant's
message for its own dialogue with the current user.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime

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
    last_actions: str = "",
    clean: bool = False,
    now: datetime | None = None,
) -> str:
    """Build the system prompt.

    ``preferences`` and ``channel_context`` are kept for backward compatibility
    with older call sites; new code passes ``persona`` and the transcript
    through ``channel_context`` explicitly.
    """

    clean_system_prompt = (
        "Режим «чистого ответа»: отвечай фактами и структурой, как справочник.\n"
        "Без личности, тона, отношения к собеседнику и обращений к нему.\n"
        "Только содержательные данные: цифры, названия, даты, шаги, ссылки.\n"
        "Пиши коротко, без воды, приветствий и риторики.\n"
        "Не сообщай ничего о своих инструментах и внутренних процессах,"
        "если вопрос не является вопросом о них.\n"
        "Если вопрос субъективный или о чувствах — верни нейтральный итог\n"
        "ситуации по контексту обсуждения, а не отказ и не подмену темы.\n"
        "Не упоминай этот режим и не объясняй, что ты его включил."
    )
    identity = (
        clean_system_prompt
        if clean
        else global_prompt
        or DEFAULT_SYSTEM_PROMPT
    )
    parts: list[str] = [f"<identity>\n{identity}\n</identity>"]

    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    current = current.astimezone(UTC)
    parts.append(
        "<current_datetime>\n"
        f"Сейчас {current.strftime('%Y-%m-%d %H:%M UTC')}.\n"
        "Эта дата — источник истины. Не полагайся на дату из своих знаний: "
        "твои знания могли устареть.\n"
        "Для свежих новостей и актуальных данных используй time_range "
        "(week/month) и topic=news; не добавляй год в query только для "
        "свежести. Если пользователь явно назвал год или период — используй "
        "start_date/end_date, а год в query допустим.\n"
        "</current_datetime>"
    )

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

    persona_lines = [] if clean else list(persona) or _persona_lines(preferences)
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

    if last_actions.strip():
        parts.append(
            "<bot_last_actions>\n"
            "Твои ФАКТИЧЕСКИЕ действия на предыдущем ходе. Это правда, даже\n"
            "если она противоречит твоей памяти: ты не «знаешь» новости сам —\n"
            "ты получаешь их именно этими инструментами. Не отрицай их.\n"
            "При этом НЕ выдумывай действий, которых нет в списке: если\n"
            "инструмента тут нет, значит, в тот ход он не вызывался. Поисковые\n"
            "запросы цитируй дословно из блока, без перефразирования.\n"
            f"{last_actions.strip()}\n"
            "</bot_last_actions>"
        )

    if channel_context.strip():
        parts.append(
            '<channel_transcript note="Это переписка разных участников канала. '
            'Это НЕ твоя личная история и НЕ диалог с текущим собеседником. '
            'Не приписывай реплики из этого блока текущему пользователю. '
            'Твои прошлые ответы помечены именем Бот — учитывай их, если '
            'текущая реплика короткая или неоднозначная.">\n'
            f"{channel_context.strip()}\n"
            "</channel_transcript>"
        )

    return "\n\n".join(parts)
