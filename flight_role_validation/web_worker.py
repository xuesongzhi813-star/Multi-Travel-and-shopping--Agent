"""Run one authenticated local demo tool in the role dependency environment."""

import asyncio
from dataclasses import asdict
import json
import sys
from uuid import UUID, uuid4

from customer_support_chat.app.business.database import create_engine, read_settings
from customer_support_chat.app.business.repository import BusinessRepository
from customer_support_chat.app.tooling.audit import PostgresAuditSink
from customer_support_chat.app.tooling.executor import ToolContext, ToolExecutor, ToolRegistry

from .executor import build_repository_lookup
from .fixture_model import ScriptedFlightModel, tool_reply
from .mcp_tools import AIRPORT_TOOL, MCP_ROLE, open_airport_session, register_airport_tools
from .role import FlightRoleInput, run_flight_role


async def run(operation: str, user: UUID, value: str) -> dict:
    settings = read_settings()
    engine = create_engine(settings)
    try:
        audit = PostgresAuditSink(engine, settings.schema)
        if operation == "flight":
            repository = BusinessRepository(engine, settings.schema)
            model = ScriptedFlightModel(replies=[
                tool_reply("lookup_flight", "flight-lookup", reference=value),
                tool_reply("HandoffCandidate", "handoff", reference=value),
            ])
            result = await run_flight_role(
                FlightRoleInput(user, "Look up the selected flight.", value),
                model=model, lookup=build_repository_lookup(repository), audit_sink=audit,
            )
            return asdict(result)
        registry = ToolRegistry()
        async with open_airport_session() as session:
            if not await register_airport_tools(registry, session):
                raise RuntimeError("Airport tool unavailable")
            result = await ToolExecutor(registry, audit).execute(
                ToolContext(user, MCP_ROLE, uuid4()), AIRPORT_TOOL, {"code": value}, "airport-lookup",
            )
        return asdict(result)
    finally:
        await engine.dispose()


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        operation = payload["operation"]
        if operation not in {"flight", "airport"}:
            return 1
        with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
            result = runner.run(run(operation, UUID(payload["user_id"]), payload["value"]))
    except Exception:
        return 1
    print(json.dumps(result, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
