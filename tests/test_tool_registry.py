from bot.tools.base import (
    Tool,
    ToolContext,
    ToolRegistry,
    enabled_tool_names,
)


def make_tool(name: str, handler) -> Tool:
    return Tool(
        name=name,
        description=name,
        parameters={"type": "object", "properties": {}, "required": []},
        handler=handler,
    )


def make_registry() -> ToolRegistry:
    registry = ToolRegistry(timeout=1.0)

    async def echo(context: ToolContext, value: str = "x") -> str:
        return f"{context.user_id}:{value}"

    registry.register(make_tool("web_search", echo))
    registry.register(make_tool("current_time", echo))
    return registry


def test_schemas_respect_enabled_list() -> None:
    registry = make_registry()
    schemas = registry.schemas(["current_time"])
    assert [item["function"]["name"] for item in schemas] == ["current_time"]


def test_enabled_tool_names_defaults_and_explicit_empty() -> None:
    registry = make_registry()
    assert enabled_tool_names(None, registry) == ["web_search"]
    assert enabled_tool_names("[]", registry) == []
    assert enabled_tool_names('["missing"]', registry) == []
    assert enabled_tool_names("not json", registry) == ["web_search"]

    only_time = ToolRegistry()

    async def noop(context: ToolContext) -> str:
        return "ok"

    only_time.register(make_tool("current_time", noop))
    assert enabled_tool_names(None, only_time) == []


async def test_execute_passes_context_and_arguments() -> None:
    registry = make_registry()
    context = ToolContext(user_id="42", channel_id="7")
    result = await registry.execute("web_search", '{"value": "hello"}', context)
    assert result == "42:hello"


async def test_unknown_tool_returns_error_string() -> None:
    registry = make_registry()
    context = ToolContext(user_id="42", channel_id="7")
    result = await registry.execute("missing", "{}", context)
    assert "недоступен" in result


async def test_tool_exception_is_returned_to_model() -> None:
    registry = ToolRegistry(timeout=1.0)

    async def broken(context: ToolContext) -> str:
        raise RuntimeError("boom")

    registry.register(make_tool("broken", broken))
    context = ToolContext(user_id="42", channel_id="7")
    result = await registry.execute("broken", "{}", context)
    assert "boom" in result
