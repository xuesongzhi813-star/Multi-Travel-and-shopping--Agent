"""Run a bounded, read-only flight role and verify its handoff."""

import asyncio
from dataclasses import dataclass
from datetime import date
import json
import re
from typing import Annotated, Literal
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from langchain.agents import create_agent
from langchain.agents.middleware import ModelCallLimitMiddleware, ToolCallLimitMiddleware, after_model
from langchain.agents.middleware.model_call_limit import ModelCallLimitExceededError
from langchain.agents.middleware.tool_call_limit import ToolCallLimitExceededError
from langchain.agents.structured_output import StructuredOutputError, ToolStrategy
from langchain.tools import ToolRuntime, tool
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from openai import APIError, APITimeoutError
from pydantic import Field, ValidationError
from customer_support_chat.app.tooling.executor import AuditSink, ToolContext, ToolExecutor, ToolRegistry
from .executor import FLIGHT_REFERENCE, FlightLookup, FlightToolData, LookupArguments, register_flight_tool


MODEL_REQUEST_LIMIT = 3
FLIGHT_LOOKUP_LIMIT = 2
SYSTEM_PROMPT = (
    "You are a read-only flight role. Use only lookup_flight, at most once per response, "
    "and wait for its tool result before another lookup. Read only the requested references. "
    "When the requested lookups are done, immediately submit HandoffCandidate for the designated "
    "reference. No text confirmation is needed."
)


class FlightProtocolError(ValueError):
    """The model response did not follow the flight role contract."""


class ModelRequestBudgetExceeded(RuntimeError):
    """A model request was prevented before reaching the provider."""


class FlightDataUnavailable(RuntimeError):
    """The flight lookup failed within the unified executor."""


class InMemoryAudit:
    def __init__(self) -> None:
        self.records = []

    async def write(self, record) -> None:
        self.records.append(record)


class HandoffCandidate(LookupArguments):
    """Indicate the one requested flight to hand off."""


@dataclass(frozen=True)
class FlightRoleInput:
    user_id: UUID
    request: str
    target_reference: str | None


@dataclass(frozen=True)
class FlightHandoff:
    reference: str
    arrival_airport: str
    arrival_date_local: date
    arrival_timezone: str
    source_tool_call_id: str


@dataclass(frozen=True)
class FlightRoleResult:
    status: Literal["verified", "needs_clarification", "unavailable"]
    reason: str | None = None
    handoff: FlightHandoff | None = None


async def run_flight_role(
    input: FlightRoleInput, *, model: BaseChatModel, lookup: FlightLookup, timeout: float = 40,
    context: ToolContext | None = None, audit_sink: AuditSink | None = None,
) -> FlightRoleResult:
    target = input.target_reference
    if target is None or re.fullmatch(FLIGHT_REFERENCE, target) is None:
        return FlightRoleResult("needs_clarification", reason="target_required")

    context = context if context is not None else ToolContext(input.user_id, "flight", uuid4())
    if context.user_id != input.user_id or context.role != "flight":
        return FlightRoleResult("unavailable", reason="protocol_error")

    registry = ToolRegistry()
    register_flight_tool(registry, lookup)
    executor = ToolExecutor(registry, audit_sink if audit_sink is not None else InMemoryAudit())
    evidence: dict[str, tuple[FlightToolData, str]] = {}

    @after_model
    async def validate_model_response(state, runtime):
        message = next((item for item in reversed(state["messages"]) if isinstance(item, AIMessage)), None)
        if message is None or message.invalid_tool_calls or len(message.tool_calls) != 1:
            raise FlightProtocolError("Expected one valid tool call")
        call = message.tool_calls[0]
        call_id = call.get("id")
        if not call_id:
            raise FlightProtocolError("Missing model tool call identifier")
        if call["name"] == "HandoffCandidate":
            LookupArguments.model_validate(call["args"])
            return None
        try:
            if call["name"] != "lookup_flight":
                raise FlightProtocolError("Unexpected model tool")
            LookupArguments.model_validate(call["args"])
        except (FlightProtocolError, ValidationError) as error:
            await executor.execute(context, call["name"], call["args"], call_id)
            raise FlightProtocolError("Rejected model tool call") from error
        return None

    @tool
    async def lookup_flight(
        reference: Annotated[str, Field(pattern=f"^{FLIGHT_REFERENCE}$")], runtime: ToolRuntime,
    ) -> str:
        """Read the authenticated user's synthetic flight by reference."""
        if not runtime.tool_call_id:
            raise FlightProtocolError("Lookup requires a tool call identifier")
        result = await executor.execute(context, "lookup_flight", {"reference": reference}, runtime.tool_call_id)
        if result.status == "not_found":
            return '{"status":"not_found"}'
        if result.status == "timeout":
            raise TimeoutError("Flight tool timed out")
        if result.status != "ok":
            raise FlightDataUnavailable("Flight tool failed")
        evidence[reference] = (FlightToolData.model_validate(result.data), result.tool_call_id)
        return json.dumps(result.data, ensure_ascii=False)

    # Explicitly prevent platform tracing from turning these role inputs into external calls.
    from langsmith import tracing_context

    try:
        with tracing_context(enabled=False):
            agent = create_agent(
                model, tools=[lookup_flight], system_prompt=SYSTEM_PROMPT,
                middleware=[
                    ModelCallLimitMiddleware(run_limit=MODEL_REQUEST_LIMIT, exit_behavior="error"),
                    ToolCallLimitMiddleware(tool_name="lookup_flight", run_limit=FLIGHT_LOOKUP_LIMIT, exit_behavior="error"),
                    validate_model_response,
                ],
                response_format=ToolStrategy(HandoffCandidate, handle_errors=False),
            )
            async with asyncio.timeout(timeout):
                result = await agent.ainvoke({"messages": [
                    {"role": "user", "content": f"{input.request}\nDesignated reference: {target}."},
                ]})
    except (TimeoutError, APITimeoutError):
        return FlightRoleResult("unavailable", reason="timeout")
    except (ModelCallLimitExceededError, ToolCallLimitExceededError, ModelRequestBudgetExceeded):
        return FlightRoleResult("unavailable", reason="budget_exhausted")
    except (FlightProtocolError, StructuredOutputError, ValidationError):
        return FlightRoleResult("unavailable", reason="protocol_error")
    except APIError:
        return FlightRoleResult("unavailable", reason="provider_error")
    except FlightDataUnavailable:
        return FlightRoleResult("unavailable", reason="flight_data_unavailable")

    candidate = result.get("structured_response")
    verified = evidence.get(target)
    if not isinstance(candidate, HandoffCandidate) or candidate.reference != target or verified is None:
        return FlightRoleResult("unavailable", reason="unverified_evidence")

    facts, tool_call_id = verified
    if facts.arrival_at_utc.utcoffset() is None:
        return FlightRoleResult("unavailable", reason="unverified_evidence")
    try:
        arrival_date = facts.arrival_at_utc.astimezone(ZoneInfo(facts.arrival_timezone)).date()
    except ZoneInfoNotFoundError:
        return FlightRoleResult("unavailable", reason="unverified_evidence")

    return FlightRoleResult("verified", handoff=FlightHandoff(
        target, facts.arrival_airport, arrival_date, facts.arrival_timezone, tool_call_id,
    ))
