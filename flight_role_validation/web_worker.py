"""Run one authenticated local workflow in the role dependency environment."""

import asyncio
from dataclasses import asdict
import json
import os
import sys
from uuid import UUID

from customer_support_chat.app.business.database import create_engine, read_settings
from customer_support_chat.app.business.repository import BusinessRepository
from customer_support_chat.app.tooling.audit import PostgresAuditSink
from customer_support_chat.app.tooling.executor import ToolContext, ToolExecutor, ToolRegistry, ToolResult

from .executor import FlightToolData, build_repository_lookup, register_flight_tool
from .mcp_tools import AIRPORT_TOOL, MCP_ROLE, open_airport_session, register_airport_tools
from .role import FlightRoleInput, handoff_from_facts, run_flight_role


async def airport(context: ToolContext, audit: PostgresAuditSink, code: str) -> ToolResult:
    registry = ToolRegistry()
    call_id = "airport-lookup"
    try:
        async with asyncio.timeout(15), open_airport_session() as session:
            if await register_airport_tools(registry, session):
                return await ToolExecutor(registry, audit).execute(context, AIRPORT_TOOL, {"code": code}, call_id)
    except TimeoutError:
        return ToolResult(call_id, "timeout", error_kind="mcp_timeout")
    except Exception:
        return ToolResult(call_id, "tool_error", error_kind="mcp_unavailable")
    return ToolResult(call_id, "tool_error", error_kind="mcp_discovery_failed")


async def flight(context: ToolContext, audit: PostgresAuditSink, repository: BusinessRepository,
                 reference: str, mode: str) -> dict:
    lookup = build_repository_lookup(repository)
    registry = ToolRegistry()
    register_flight_tool(registry, lookup)
    owned = await ToolExecutor(registry, audit).execute(context, "lookup_flight", {"reference": reference}, "flight-lookup")
    if owned.status != "ok":
        status = "not_found" if owned.status == "not_found" else "unavailable"
        return {"status": status, "reason": owned.status, "handoff": None, "mode": mode, "provider_requests": 0}

    provider_requests = 0
    lookup_selected = restrict_lookup(lookup, reference)
    role_input = FlightRoleInput(context.user_id, f"Look up {reference}, then submit HandoffCandidate.", reference)
    if mode == "deepseek":
        from .live import open_live_model

        async with open_live_model(os.environ["DEEPSEEK_API_KEY"]) as (model, budget):
            result = await run_flight_role(
                role_input, model=model, lookup=lookup_selected, context=context, audit_sink=audit, timeout=36,
            )
            provider_requests = budget.attempts
    elif mode == "offline":
        from .fixture_model import ScriptedFlightModel, tool_reply

        model = ScriptedFlightModel(replies=[
            tool_reply("lookup_flight", "offline-flight-lookup", reference=reference),
            tool_reply("HandoffCandidate", "offline-handoff", reference=reference),
        ])
        result = await run_flight_role(role_input, model=model, lookup=lookup_selected, context=context, audit_sink=audit)
    else:
        result = handoff_from_facts(FlightToolData.model_validate(owned.data), owned.tool_call_id)

    report = {**asdict(result), "mode": mode, "provider_requests": provider_requests}
    if result.handoff is not None:
        airport_context = ToolContext(context.user_id, MCP_ROLE, context.run_id)
        report["airport"] = asdict(await airport(airport_context, audit, result.handoff.arrival_airport))
    return report


def restrict_lookup(lookup, reference):
    async def selected(user_id: UUID, requested: str):
        return await lookup(user_id, requested) if requested == reference else None
    return selected


async def run(operation: str, user_id: UUID, value: str, request_id: UUID, mode: str) -> dict:
    settings = read_settings()
    engine = create_engine(settings)
    try:
        audit = PostgresAuditSink(engine, settings.schema)
        if operation == "flight":
            return await flight(
                ToolContext(user_id, "flight", request_id), audit, BusinessRepository(engine, settings.schema), value, mode,
            )
        return asdict(await airport(ToolContext(user_id, MCP_ROLE, request_id), audit, value))
    finally:
        await engine.dispose()


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        operation, mode = payload["operation"], payload["flight_mode"]
        if operation not in {"flight", "airport"} or mode not in {"direct", "offline", "deepseek"}:
            return 1
        with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
            result = runner.run(run(
                operation, UUID(payload["user_id"]), payload["value"], UUID(payload["request_id"]), mode,
            ))
    except Exception:
        return 1
    print(json.dumps(result, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
