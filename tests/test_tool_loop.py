from types import SimpleNamespace

from discord_bot.citations import Citation
from discord_bot.llm.errors import ErrorKind, LLMError
from discord_bot.llm.models import ModelRef
from discord_bot.llm.providers import ProviderSpec
from discord_bot.llm.tool_loop import run_tool_loop
from discord_bot.tools.base import Tool, ToolContext, ToolRegistry

SPEC = ProviderSpec(
    name="groq",
    base_url="https://example.test",
    api_key_env="GROQ_API_KEY",
    default_model="model",
    default_utility_model="fast",
)
MODEL_REF = ModelRef(provider="groq", model="model", utility_model="fast")
CONTEXT = ToolContext(user_id="1", channel_id="2")


def response(content: str | None = None, tool_calls: list | None = None):
    message = SimpleNamespace(content=content, tool_calls=tool_calls)
    return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def tool_call(call_id: str, name: str, arguments: str):
    return SimpleNamespace(
        id=call_id,
        function=SimpleNamespace(name=name, arguments=arguments),
    )


class FakeLLM:
    def __init__(self, responses: list) -> None:
        self.responses = list(responses)
        self.calls: list[dict] = []

    async def complete(self, spec, model, messages, **kwargs):
        self.calls.append({"messages": messages, **kwargs})
        return self.responses.pop(0)


def make_registry() -> ToolRegistry:
    registry = ToolRegistry(timeout=1.0)

    async def double(context: ToolContext, value: int = 0) -> str:
        return str(value * 2)

    registry.register(
        Tool(
            name="double",
            description="double a number",
            parameters={
                "type": "object",
                "properties": {"value": {"type": "integer"}},
                "required": ["value"],
            },
            handler=double,
        )
    )
    return registry


async def test_tool_call_is_executed_and_answered() -> None:
    llm = FakeLLM(
        [
            response(tool_calls=[tool_call("call-1", "double", '{"value": 21}')]),
            response(content="done"),
        ]
    )
    messages: list[dict] = [{"role": "user", "content": "hi"}]

    result = await run_tool_loop(
        llm, SPEC, MODEL_REF, messages, make_registry(), ["double"], CONTEXT
    )

    assert result == "done"
    tool_messages = [item for item in messages if item["role"] == "tool"]
    assert len(tool_messages) == 1
    assert tool_messages[0]["content"] == "42"
    assert tool_messages[0]["tool_call_id"] == "call-1"


async def test_unknown_tool_still_gets_a_tool_response() -> None:
    llm = FakeLLM(
        [
            response(tool_calls=[tool_call("call-1", "missing", "{}")]),
            response(content="recovered"),
        ]
    )
    messages: list[dict] = []

    result = await run_tool_loop(
        llm, SPEC, MODEL_REF, messages, make_registry(), ["double"], CONTEXT
    )

    assert result == "recovered"
    tool_messages = [item for item in messages if item["role"] == "tool"]
    assert "недоступен" in tool_messages[0]["content"]


async def test_final_call_without_tools_after_round_limit() -> None:
    llm = FakeLLM(
        [
            response(tool_calls=[tool_call("call-1", "double", '{"value": 2}')]),
            response(content="final"),
        ]
    )

    result = await run_tool_loop(
        llm,
        SPEC,
        MODEL_REF,
        [],
        make_registry(),
        ["double"],
        CONTEXT,
        max_rounds=1,
    )

    assert result == "final"
    assert llm.calls[-1]["tools"] is None


async def test_forced_tool_is_requested_on_the_first_round() -> None:
    llm = FakeLLM(
        [
            response(tool_calls=[tool_call("call-1", "double", '{"value": 3}')]),
            response(content="done"),
        ]
    )

    result = await run_tool_loop(
        llm,
        SPEC,
        MODEL_REF,
        [],
        make_registry(),
        ["double"],
        CONTEXT,
        forced_tool="double",
    )

    assert result == "done"
    assert llm.calls[0]["tool_choice"] == {
        "type": "function",
        "function": {"name": "double"},
    }
    assert llm.calls[1]["tool_choice"] == "auto"


async def test_forced_tool_is_ignored_when_no_schemas_are_enabled() -> None:
    llm = FakeLLM([response(content="plain")])

    result = await run_tool_loop(
        llm,
        SPEC,
        MODEL_REF,
        [],
        make_registry(),
        [],
        CONTEXT,
        forced_tool="double",
    )

    assert result == "plain"
    assert llm.calls[0]["tool_choice"] == "auto"


async def test_forced_tool_falls_back_to_auto_on_bad_request() -> None:
    class RejectingLLM(FakeLLM):
        async def complete(self, spec, model, messages, **kwargs):
            if kwargs.get("tool_choice") != "auto":
                self.calls.append({"messages": messages, **kwargs})
                raise LLMError(
                    provider="groq",
                    model="model",
                    status_code=400,
                    kind=ErrorKind.bad_request,
                    detail="tool_choice is not supported",
                )
            return await super().complete(spec, model, messages, **kwargs)

    llm = RejectingLLM([response(content="fallback")])

    result = await run_tool_loop(
        llm,
        SPEC,
        MODEL_REF,
        [],
        make_registry(),
        ["double"],
        CONTEXT,
        forced_tool="double",
    )

    assert result == "fallback"
    assert llm.calls[-1]["tool_choice"] == "auto"


async def test_tool_context_collects_citations() -> None:
    registry = ToolRegistry(timeout=1.0)

    async def search(context: ToolContext, query: str) -> str:
        context.citations.append(
            Citation(title="Source", url="https://example.com/source")
        )
        return "result"

    registry.register(
        Tool(
            name="search",
            description="search",
            parameters={"type": "object", "properties": {"query": {"type": "string"}}},
            handler=search,
        )
    )

    llm = FakeLLM(
        [
            response(tool_calls=[tool_call("call-1", "search", '{"query": "x"}')]),
            response(content="answer"),
        ]
    )
    context = ToolContext(user_id="1", channel_id="2")

    await run_tool_loop(llm, SPEC, MODEL_REF, [], registry, ["search"], context)

    assert len(context.citations) == 1
    assert context.citations[0].url == "https://example.com/source"
