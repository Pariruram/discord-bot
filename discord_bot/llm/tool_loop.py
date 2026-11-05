"""Safe multi-turn tool execution loop."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

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
) -> str:
    """Run the model, execute requested tools and feed results back.

    Every tool call always gets a matching ``tool`` message, including unknown
    tools and failures. This keeps the conversation valid for strict providers.
    """

    tool_schemas = registry.schemas(enabled_tools) or None

    for _ in range(max(1, max_rounds)):
        response = await llm.complete(
            spec,
            model_ref.model,
            messages,
            tools=tool_schemas,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        message = response.choices[0].message
        tool_calls = getattr(message, "tool_calls", None) or []

        if not tool_calls:
            content = (getattr(message, "content", None) or "").strip()
            return content or fallback

        assistant_message: dict[str, Any] = {
            "role": "assistant",
            "content": getattr(message, "content", None) or "",
            "tool_calls": [_serialize_tool_call(call) for call in tool_calls],
        }
        messages.append(assistant_message)

        for tool_call in tool_calls:
            name = tool_call.function.name
            logger.info("Вызов инструмента: %s", name)
            result = await registry.execute(name, tool_call.function.arguments, context)
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
    )
    content = (getattr(final.choices[0].message, "content", None) or "").strip()
    return content or fallback
