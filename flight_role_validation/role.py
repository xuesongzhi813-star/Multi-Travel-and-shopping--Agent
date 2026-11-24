"""Run a bounded, read-only flight role and verify its handoff."""

import asyncio
from dataclasses import dataclass, replace
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
from openai import APIError, APITimeoutError, RateLimitError
from pydantic import Field, ValidationError
from customer_support_chat.app.tooling.executor import (
    AuditSink, ToolContext, ToolExecutor, ToolRegistry, valid_tool_call_id,
)
from faq_extension.contracts import FaqSearchResult, PublicFaqQuestion
from .public_faq import FaqLookup, FaqRoleCall
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
    faq: FaqSearchResult | None = None


async def run_flight_role(
    input: FlightRoleInput, *, model: BaseChatModel, lookup: FlightLookup, timeout: float = 40,
    context: ToolContext | None = None, audit_sink: AuditSink | None = None,
    faq_question: PublicFaqQuestion | None = None, faq_lookup: FaqLookup | None = None,
) -> FlightRoleResult:
    target = input.target_reference
    if target is None or re.fullmatch(FLIGHT_REFERENCE, target) is None:
        return FlightRoleResult("needs_clarification", reason="target_required")

    context = context if context is not None else ToolContext(input.user_id, "flight", uuid4())
    if context.user_id != input.user_id or context.role != "flight":
        return FlightRoleResult("unavailable", reason="protocol_error")

    registry = ToolRegistry()
    register_flight_tool(registry, lookup)
    faq = FaqRoleCall(registry, faq_question, faq_lookup)
    audit = audit_sink if audit_sink is not None else InMemoryAudit()
    executor = ToolExecutor(registry, audit)
    faq_executor = ToolExecutor(registry, audit, timeout=6)
    evidence: dict[str, tuple[FlightToolData, str]] = {}

    @after_model
    async def validate_model_response(state, runtime):
        message = next((item for item in reversed(state["messages"]) if isinstance(item, AIMessage)), None)
        if message is None:
            raise FlightProtocolError("Expected one valid tool call")
        if message.invalid_tool_calls or len(message.tool_calls) != 1:
            for proposal in [*message.tool_calls, *message.invalid_tool_calls]:
                name = proposal.get("name") or ""
                if name != "HandoffCandidate":
                    await executor.reject_envelope(context, name, proposal.get("id") or "")
            raise FlightProtocolError("Expected one valid tool call")
        call = message.tool_calls[0]
        call_id = call.get("id")
        if not valid_tool_call_id(call_id):
            raise FlightProtocolError("Invalid model tool call identifier")
        if faq.accepts(call):
            return None
        if call["name"] == "HandoffCandidate":
            LookupArguments.model_validate(call["args"])
            return None
        try:
            if call["name"] != "lookup_flight":
                await executor.reject_envelope(context, call["name"], call_id)
                raise FlightProtocolError("Unexpected model tool")
            LookupArguments.model_validate(call["args"])
        except ValidationError as error:
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

    @tool
    async def search_public_faq(runtime: ToolRuntime) -> str:
        """Read evidence for the public policy question supplied by the host."""
        return await faq.invoke(faq_executor, context, runtime.tool_call_id)

    tools = [lookup_flight, search_public_faq] if faq.enabled else [lookup_flight]
    prompt = SYSTEM_PROMPT
    if faq.enabled:
        prompt += " The host also requests one call to search_public_faq before handoff."

    # Explicitly prevent platform tracing from turning these role inputs into external calls.
    from langsmith import tracing_context

    try:
        with tracing_context(enabled=False):
            agent = create_agent(
                model, tools=tools, system_prompt=prompt,
                middleware=[
                    ModelCallLimitMiddleware(run_limit=MODEL_REQUEST_LIMIT + int(faq.enabled), exit_behavior="error"),
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
    except RateLimitError:
        return FlightRoleResult("unavailable", reason="provider_rate_limit")
    except APIError:
        return FlightRoleResult("unavailable", reason="provider_error")
    except FlightDataUnavailable:
        return FlightRoleResult("unavailable", reason="flight_data_unavailable")

    candidate = result.get("structured_response")
    verified = evidence.get(target)
    if not isinstance(candidate, HandoffCandidate) or candidate.reference != target or verified is None:
        return FlightRoleResult("unavailable", reason="unverified_evidence")

    return replace(handoff_from_facts(*verified), faq=faq.result)


def handoff_from_facts(facts: FlightToolData, tool_call_id: str) -> FlightRoleResult:
    if facts.arrival_at_utc.utcoffset() is None:
        return FlightRoleResult("unavailable", reason="unverified_evidence")
    try:
        arrival_date = facts.arrival_at_utc.astimezone(ZoneInfo(facts.arrival_timezone)).date()
    except ZoneInfoNotFoundError:
        return FlightRoleResult("unavailable", reason="unverified_evidence")

    return FlightRoleResult("verified", handoff=FlightHandoff(
        facts.reference, facts.arrival_airport, arrival_date, facts.arrival_timezone, tool_call_id,
    ))
