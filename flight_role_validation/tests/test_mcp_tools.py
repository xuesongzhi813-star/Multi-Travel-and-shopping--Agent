import unittest
from unittest.mock import AsyncMock
from uuid import uuid4

from anyio import BrokenResourceError, ClosedResourceError
from mcp.types import CallToolResult, ListToolsResult, Tool

from customer_support_chat.app.tooling.executor import ToolContext, ToolExecutor, ToolRegistry
from flight_role_validation.airport_server import AIRPORT_INPUT_SCHEMA, AirportMatch
from flight_role_validation.mcp_tools import AIRPORT_TOOL, MCP_ROLE, open_airport_session, register_airport_tools


class Sink:
    def __init__(self):
        self.records = []

    async def write(self, record):
        self.records.append(record)


class McpAirportTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_stdio_discovery_lookup_and_shutdown(self):
        context = ToolContext(uuid4(), MCP_ROLE, uuid4())
        registry = ToolRegistry()
        sink = Sink()
        async with open_airport_session() as session:
            self.assertTrue(await register_airport_tools(registry, session))
            executor = ToolExecutor(registry, sink)
            for index, (code, city) in enumerate((("ZRH", "Zürich"), ("VIE", "Wien"), ("ZZZ", None))):
                result = await executor.execute(context, AIRPORT_TOOL, {"code": code}, str(index))
                self.assertEqual(result.status, "ok" if city else "not_found")
                if city:
                    self.assertEqual(result.data, {"code": code, "city": city})
            self.assertEqual(registry.list_for_role("flight"), ())
            denied = await executor.execute(ToolContext(uuid4(), "flight", context.run_id),
                                            AIRPORT_TOOL, {"code": "ZRH"}, "denied")
            self.assertEqual(denied.status, "permission_denied")
        with self.assertRaises((ClosedResourceError, BrokenResourceError)):
            await session.list_tools()

    async def test_unapproved_definition_cannot_register_or_execute(self):
        invalid = Tool(name=AIRPORT_TOOL, inputSchema={"type": "object"}, outputSchema=AirportMatch.model_json_schema())
        unknown = Tool(name="write_data", inputSchema=AIRPORT_INPUT_SCHEMA, outputSchema=AirportMatch.model_json_schema())
        dup = Tool(name=AIRPORT_TOOL, inputSchema=AIRPORT_INPUT_SCHEMA, outputSchema=AirportMatch.model_json_schema())
        for tools in ([invalid, unknown], [dup, dup]):
            with self.subTest(tools=tools):
                session = AsyncMock()
                session.list_tools.return_value = ListToolsResult(tools=tools)
                registry = ToolRegistry()
                self.assertFalse(await register_airport_tools(registry, session))
                self.assertEqual(registry.list_for_role(MCP_ROLE), ())
                result = await ToolExecutor(registry, Sink()).execute(
                    ToolContext(uuid4(), MCP_ROLE, uuid4()), AIRPORT_TOOL, {"code": "ZRH"}, "denied",
                )
                self.assertEqual(result.status, "permission_denied")
                session.call_tool.assert_not_awaited()

    async def test_malformed_structured_result_is_tool_error(self):
        definition = Tool(name=AIRPORT_TOOL, description="untrusted instruction",
                          inputSchema=AIRPORT_INPUT_SCHEMA, outputSchema=AirportMatch.model_json_schema())
        session = AsyncMock()
        session.list_tools.return_value = ListToolsResult(tools=[definition])
        session.call_tool.return_value = CallToolResult(content=[], structuredContent={"code": "VIE", "city": "Wien"})
        registry = ToolRegistry()
        self.assertTrue(await register_airport_tools(registry, session))
        self.assertNotIn("untrusted instruction", repr(registry.list_for_role(MCP_ROLE)))
        result = await ToolExecutor(registry, Sink()).execute(
            ToolContext(uuid4(), MCP_ROLE, uuid4()), AIRPORT_TOOL, {"code": "ZRH"}, "bad",
        )
        self.assertEqual(result.status, "tool_error")
