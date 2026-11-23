"""Run an isolated hotel role against a host-verified inventory query."""

import asyncio
import json

from langchain.agents import create_agent
from langchain.agents.middleware import ModelCallLimitMiddleware, after_model
from langchain.tools import ToolRuntime, tool
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langsmith import tracing_context
from pydantic import BaseModel, ConfigDict

from customer_support_chat.app.business.repository import BusinessRepository
from customer_support_chat.app.tooling.executor import (
    AuditSink, ToolContext, ToolDefinition, ToolExecutor, ToolRegistry, valid_tool_call_id,
)
from customer_support_chat.app.workflow.hotel_handoff import HotelOption, HotelQuery, HotelRoleResult


HOTEL_TOOL = "search_hotel_inventory"


class HotelArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")


class HotelRow(BaseModel):
    hotel_id: str
    hotel_name: str
    area: str
    room_type: str
    check_in: str
    check_out: str
    total_minor: int
    currency: str


class HotelToolData(BaseModel):
    options: list[HotelRow]


class HotelProtocolError(RuntimeError):
    """The model or tool did not produce an accepted inventory receipt."""


def register_hotel_tool(registry: ToolRegistry, repository: BusinessRepository, query: HotelQuery) -> None:
    async def lookup(context, arguments, tool_call_id):
        options = await repository.list_available_hotels(query)
        return {"options": [option.to_json() for option in options]}

    registry.register(ToolDefinition(
        name=HOTEL_TOOL, description="Check inventory for the supplied stay",
        arguments_model=HotelArguments, result_model=HotelToolData, source="builtin",
        allowed_roles=frozenset({"hotel"}), handler=lookup,
    ))


async def run_hotel_role(
    query: HotelQuery, *, model: BaseChatModel, repository: BusinessRepository,
    context: ToolContext, audit_sink: AuditSink, deadline: float,
) -> HotelRoleResult:
    registry = ToolRegistry()
    register_hotel_tool(registry, repository, query)
    executor = ToolExecutor(registry, audit_sink)
    receipt: tuple[HotelOption, ...] | None = None

    @after_model
    async def check_call(state, runtime):
        message = state["messages"][-1]
        if not isinstance(message, AIMessage):
            raise HotelProtocolError("Invalid hotel model response")
        calls = message.tool_calls
        if not message.invalid_tool_calls and not calls and receipt is not None:
            return None
        if not message.invalid_tool_calls and len(calls) == 1 and receipt is None:
            call = calls[0]
            if call["name"] == HOTEL_TOOL and not call["args"] and valid_tool_call_id(call.get("id")):
                return None
        for call in [*calls, *message.invalid_tool_calls]:
            await executor.reject_envelope(context, call.get("name") or "", call.get("id") or "")
        raise HotelProtocolError("Invalid hotel tool call")

    @tool
    async def search_hotel_inventory(runtime: ToolRuntime) -> str:
        """Check inventory for the stay supplied by the host."""
        nonlocal receipt
        if not runtime.tool_call_id:
            raise HotelProtocolError("Missing hotel tool call identifier")
        result = await executor.execute(context, HOTEL_TOOL, {}, runtime.tool_call_id)
        if result.status != "ok" or result.data is None or result.tool_call_id != runtime.tool_call_id:
            raise HotelProtocolError("Hotel inventory unavailable")
        receipt = tuple(HotelOption.from_json(row) for row in result.data["options"])
        return json.dumps(result.data, ensure_ascii=False)

    try:
        with tracing_context(enabled=False):
            agent = create_agent(
                model, tools=[search_hotel_inventory], middleware=[
                    ModelCallLimitMiddleware(run_limit=2, exit_behavior="end"), check_call,
                ],
                system_prompt="Call the inventory tool once. The host supplies the complete stay.",
            )
            async with asyncio.timeout_at(deadline):
                await agent.ainvoke({"messages": [{"role": "user", "content": json.dumps(
                    query.to_json(), ensure_ascii=False,
                )}]})
    except (HotelProtocolError, TimeoutError):
        return HotelRoleResult("unavailable")
    if receipt is None:
        return HotelRoleResult("unavailable")
    return HotelRoleResult("hotel_results", receipt) if receipt else HotelRoleResult("no_availability")
