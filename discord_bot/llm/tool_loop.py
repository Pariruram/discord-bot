"""Safe multi-turn tool execution loop."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from discord_bot.llm.errors import ErrorKind, LLMError
from discord_bot.llm.models import ModelRef
from discord_bot.llm.providers import ProviderSpec
from discord_bot.tools.base import ToolContext, ToolRegistry

if TYPE_CHECKING:
    from discord_bot.llm.client import LLMClient

logger = logging.getLogger(__name__)


def _serialize_tool_call(tool_call: Any) -> dict[str, Any]:
    if hasattr(tool_call, "model_dump"):
        return tool_call.model_dump(exclude_none=True)

    return {
        "id": getattr(tool_call, "id", ""),
        "type": "function",
        "function": {
            "name": tool_call.function.name,
            "arguments": tool_call.function.arguments,
        },
    }


def _call_key(name: str, raw_args: str) -> tuple[str, str]:
    """Normalise a tool call so the same request is detected reliably."""

    try:
        parsed = json.loads(raw_args or "{}")
    except (TypeError, ValueError):
        return name, raw_args or ""
    return name, json.dumps(parsed, sort_keys=True, ensure_ascii=False)


def _finish(
    content: str | None, context: ToolContext, fallback: str
) -> str:
    text = (content or "").strip()
    if text:
        return text
    if context.files:
        # The drawing (or another generated file) already exists; losing the
        # model's caption must not turn it into "failed to form a reply".
        return "Готово — файл прикреплён к ответу."
    return fallback


async def run_tool_loop(
    llm: LLMClient,
    spec: ProviderSpec,
    model_ref: ModelRef,
    messages: list[dict[str, Any]],
    registry: ToolRegistry,
    enabled_tools: list[str],
    context: ToolContext,
    *,
    max_rounds: int = 4,
    temperature: float = 0.75,
    max_tokens: int = 1200,
    fallback: str = "Извини, не удалось сформировать ответ.",
    forced_tool: str | None = None,
) -> str:
    """Run the model, execute requested tools and feed results back.

    Every tool call always gets a matching ``tool`` message, including unknown
    tools and failures. This keeps the conversation valid for strict providers.
    """

    tool_schemas = registry.schemas(enabled_tools) or None
    seen_calls: set[tuple[str, str]] = set()

    for round_index in range(max(1, max_rounds)):
        choice: str | dict[str, Any] = "auto"
        if round_index == 0 and forced_tool and tool_schemas:
            choice = {
                "type": "function",
                "function": {"name": forced_tool},
            }
        response = None
        try:
            response = await llm.complete(
                spec,
                model_ref.model,
                messages,
                tools=tool_schemas,
                temperature=temperature,
                max_tokens=max_tokens,
                tool_choice=choice,
                purpose="chat",
            )
        except LLMError as exc:
            if choice != "auto" and exc.kind is ErrorKind.bad_request:
                # Some providers reject a named tool_choice; let the model pick.
                logger.warning(
                    "Провайдер отклонил принудительный tool_choice, повторяю "
                    "в режиме auto: %s",
                    exc,
                )
                response = await llm.complete(
                    spec,
                    model_ref.model,
                    messages,
                    tools=tool_schemas,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    tool_choice="auto",
                    purpose="chat",
                )
            else:
                raise
        message = response.choices[0].message
        tool_calls = getattr(message, "tool_calls", None) or []

        if not tool_calls:
            content = getattr(message, "content", None)
            return _finish(content, context, fallback)

        assistant_message: dict[str, Any] = {
            "role": "assistant",
            "content": getattr(message, "content", None) or "",
            "tool_calls": [_serialize_tool_call(call) for call in tool_calls],
        }
        messages.append(assistant_message)

        for tool_call in tool_calls:
            name = tool_call.function.name
            raw_args = getattr(tool_call.function, "arguments", "") or ""
            key = _call_key(name, raw_args)
            if key in seen_calls:
                logger.warning(
                    "Повторный вызов инструмента %s с теми же аргументами "
                    "пропущен",
                    name,
                )
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": getattr(tool_call, "id", ""),
                        "name": name,
                        "content": (
                            f"Повторный вызов {name} с теми же аргументами "
                            "не выполнен: инструмент уже вызывался в этом "
                            "ответе. Используй уже полученный результат и не "
                            "вызывай его снова."
                        ),
                    }
                )
                continue
            seen_calls.add(key)
            logger.info("Вызов инструмента: %s", name)
            result = await registry.execute(name, raw_args, context)
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": getattr(tool_call, "id", ""),
                    "name": name,
                    "content": result,
                }
            )

    logger.warning("Достигнут лимит раундов инструментов (%s)", max_rounds)
    final = await llm.complete(
        spec,
        model_ref.model,
        messages,
        tools=None,
        temperature=temperature,
        max_tokens=max_tokens,
        purpose="chat_final",
    )
    content = getattr(final.choices[0].message, "content", None)
    return _finish(content, context, fallback)
