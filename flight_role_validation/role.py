"""Run a bounded, read-only flight role and verify its handoff."""

import asyncio
from dataclasses import dataclass
from datetime import date
import json
import re
from typing import Annotated, Literal
from uuid import UUID
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
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy.exc import SQLAlchemyError

from .executor import FLIGHT_REFERENCE, FlightExecutor, FlightLookup


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


class LookupArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reference: Annotated[str, Field(pattern=f"^{FLIGHT_REFERENCE}$")]


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


@after_model
def validate_model_response(state, runtime):
    message = next((item for item in reversed(state["messages"]) if isinstance(item, AIMessage)), None)
    if message is None or message.invalid_tool_calls or len(message.tool_calls) != 1:
        raise FlightProtocolError("Expected one valid tool call")
    call = message.tool_calls[0]
    if call["name"] not in {"lookup_flight", "HandoffCandidate"} or not call.get("id"):
        raise FlightProtocolError("Unexpected model tool")
    LookupArguments.model_validate(call["args"])
    return None


async def run_flight_role(
    input: FlightRoleInput, *, model: BaseChatModel, lookup: FlightLookup, timeout: float = 40,
) -> FlightRoleResult:
    target = input.target_reference
    if target is None or re.fullmatch(FLIGHT_REFERENCE, target) is None:
        return FlightRoleResult("needs_clarification", reason="target_required")

    executor = FlightExecutor(input.user_id, lookup)

    @tool
    async def lookup_flight(
        reference: Annotated[str, Field(pattern=f"^{FLIGHT_REFERENCE}$")], runtime: ToolRuntime,
    ) -> str:
        """Read the authenticated user's synthetic flight by reference."""
        if not runtime.tool_call_id:
            raise FlightProtocolError("Lookup requires a tool call identifier")
        facts = await executor.lookup_flight(reference, runtime.tool_call_id)
        if facts is None:
            return '{"status":"not_found"}'
        return json.dumps({
            "reference": facts.reference,
            "arrival_airport": facts.arrival_airport,
            "arrival_at_utc": facts.arrival_at_utc.isoformat(),
            "arrival_timezone": facts.arrival_timezone,
        })

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
    except SQLAlchemyError:
        return FlightRoleResult("unavailable", reason="flight_data_unavailable")

    candidate = result.get("structured_response")
    evidence = executor.evidence(target)
    if not isinstance(candidate, HandoffCandidate) or candidate.reference != target or evidence is None:
        return FlightRoleResult("unavailable", reason="unverified_evidence")

    facts = evidence.facts
    if facts.arrival_at_utc.utcoffset() is None:
        return FlightRoleResult("unavailable", reason="unverified_evidence")
    try:
        arrival_date = facts.arrival_at_utc.astimezone(ZoneInfo(facts.arrival_timezone)).date()
    except ZoneInfoNotFoundError:
        return FlightRoleResult("unavailable", reason="unverified_evidence")

    return FlightRoleResult("verified", handoff=FlightHandoff(
        target, facts.arrival_airport, arrival_date, facts.arrival_timezone, evidence.tool_call_id,
    ))
