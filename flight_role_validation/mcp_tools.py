"""Allow and call only the fixed local airport MCP tool."""

import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
import sys
from typing import Annotated, Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from pydantic import BaseModel, ConfigDict, Field

from customer_support_chat.app.tooling.executor import ToolDefinition, ToolRegistry
from .airport_server import AIRPORT_CODE_PATTERN, AIRPORT_INPUT_SCHEMA, SERVER_NAME, AirportMatch


AIRPORT_TOOL = "lookup_airport_city"
MCP_ROLE = "p5_mcp_validation"
MAX_DISCOVERED = 16
SCHEMA_ANNOTATIONS = {"title", "description", "examples", "$comment"}


class AirportArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    code: Annotated[str, Field(pattern=AIRPORT_CODE_PATTERN)]


def normalized_schema(value: Any) -> Any:
    if isinstance(value, list):
        return [normalized_schema(item) for item in value]
    if not isinstance(value, dict):
        return value
    result = {}
    for key, entry in value.items():
        if key in SCHEMA_ANNOTATIONS:
            continue
        if key in {"properties", "patternProperties", "$defs"} and isinstance(entry, dict):
            result[key] = {name: normalized_schema(schema) for name, schema in entry.items()}
        elif key == "required" and isinstance(entry, list) and all(isinstance(item, str) for item in entry):
            result[key] = sorted(entry)
        else:
            result[key] = normalized_schema(entry)
    return result


@asynccontextmanager
async def open_airport_session():
    parameters = StdioServerParameters(
        command=sys.executable, args=["-B", "-m", "flight_role_validation.airport_server"],
        cwd=Path(__file__).resolve().parents[1], env={"PYTHONUTF8": "1"},
    )
    async with stdio_client(parameters) as (read, write):
        async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=8)) as session:
            async with asyncio.timeout(8):
                initialized = await session.initialize()
            if initialized.serverInfo.name != SERVER_NAME:
                raise ValueError("Unrecognized airport tool server")
            yield session


async def register_airport_tools(registry: ToolRegistry, session: ClientSession) -> bool:
    discovered = []
    cursor = None
    cursors = set()
    async with asyncio.timeout(8):
        while True:
            page = await session.list_tools() if cursor is None else await session.list_tools(cursor=cursor)
            discovered.extend(page.tools)
            if len(discovered) > MAX_DISCOVERED:
                return False
            cursor = page.nextCursor
            if cursor is None:
                break
            if cursor in cursors or len(cursors) >= MAX_DISCOVERED:
                return False
            cursors.add(cursor)
    matching = [tool for tool in discovered if tool.name == AIRPORT_TOOL]
    if len(matching) != 1 or registry.get(AIRPORT_TOOL) is not None:
        return False
    tool = matching[0]
    if normalized_schema(tool.inputSchema) != normalized_schema(AIRPORT_INPUT_SCHEMA):
        return False
    if normalized_schema(tool.outputSchema) != normalized_schema(AirportMatch.model_json_schema()):
        return False

    async def invoke(context, arguments, tool_call_id):
        response = await session.call_tool(AIRPORT_TOOL, arguments={"code": arguments.code})
        if response.isError:
            raise ValueError("Airport MCP tool returned an error")
        match = AirportMatch.model_validate(response.structuredContent, strict=True)
        if match.code != arguments.code:
            raise ValueError("Airport MCP returned another code")
        return match.model_dump() if match.city is not None else None

    registry.register(ToolDefinition(
        AIRPORT_TOOL, "Look up a city by its airport code", AirportArguments, AirportMatch,
        "mcp:travel-airport-demo", frozenset({MCP_ROLE}), invoke,
        audit_argument_fields=frozenset({"code"}),
    ))
    return True
