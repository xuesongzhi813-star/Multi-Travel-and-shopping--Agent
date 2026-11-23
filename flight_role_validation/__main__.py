"""Run the offline flight-role fixture or one explicitly enabled live validation."""

import argparse
import asyncio
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import asdict, replace
from datetime import date, datetime
import json
import os
from secrets import token_urlsafe
from uuid import UUID, uuid4

from sqlalchemy.schema import DropSchema

from customer_support_chat.app.business.database import create_engine, migrate_business, read_settings
from customer_support_chat.app.business.demo_seed import DEMO_USERS, FLIGHTS, seed_demo
from customer_support_chat.app.business.repository import BusinessRepository
from customer_support_chat.app.tooling.audit import PostgresAuditSink
from .executor import FlightFacts, build_repository_lookup
from .fixture_model import ScriptedFlightModel, synthetic_replies
from .live import MODEL_NAME, open_live_model
from .role import FlightRoleInput, run_flight_role


LIVE_TIMEOUT = 40
CLEANUP_RESERVE = 5
DEMO_USER = DEMO_USERS[0][0]
SYNTHETIC_INPUT = FlightRoleInput(
    DEMO_USER,
    "First call lookup_flight for DEMO-A1. After its result, call lookup_flight for DEMO-A2. "
    "After the second result, hand off DEMO-A1 via HandoffCandidate.",
    "DEMO-A1",
)


async def synthetic_lookup(user_id: UUID, reference: str) -> FlightFacts | None:
    if user_id != DEMO_USER:
        return None
    for flight_ref, owner, _, _, destination, _, utc_arrival, timezone in FLIGHTS:
        if owner == 0 and flight_ref == reference:
            return FlightFacts(flight_ref, destination, datetime.fromisoformat(utc_arrival), timezone)
    return None


@asynccontextmanager
async def temporary_repository(work_deadline: float) -> AsyncIterator[BusinessRepository]:
    config = replace(read_settings(), schema=f"p4_live_{uuid4().hex}")
    engine = create_engine(config)
    try:
        async with asyncio.timeout_at(work_deadline):
            await migrate_business(engine, config.schema)
            await seed_demo(engine, config.schema, (token_urlsafe(24), token_urlsafe(24)))
            yield BusinessRepository(engine, config.schema)
    finally:
        try:
            async with engine.begin() as connection:
                await connection.execute(DropSchema(config.schema, cascade=True, if_exists=True))
        finally:
            await engine.dispose()


def write_report(report: dict) -> None:
    print(json.dumps(report, ensure_ascii=False, default=str), flush=True)


async def run_offline_validation() -> int:
    model = ScriptedFlightModel(replies=synthetic_replies())
    result = await run_flight_role(SYNTHETIC_INPUT, model=model, lookup=synthetic_lookup)
    write_report({"mode": "offline", "provider_requests": 0, "result": asdict(result)})
    return 0 if result.status == "verified" else 1


async def run_live_validation() -> int:
    api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        write_report({"mode": "live", "provider_requests": 0, "error": "missing_key"})
        return 1

    report = {"mode": "live", "model": MODEL_NAME, "provider_requests": 0, "passed": False}
    deadline = asyncio.get_running_loop().time() + LIVE_TIMEOUT
    work_deadline = deadline - min(CLEANUP_RESERVE, LIVE_TIMEOUT / 8)
    try:
        async with asyncio.timeout_at(deadline):
            async with temporary_repository(work_deadline) as repository, open_live_model(api_key) as (model, budget):
                try:
                    result = await run_flight_role(
                        SYNTHETIC_INPUT, model=model, lookup=build_repository_lookup(repository),
                        audit_sink=PostgresAuditSink(repository.engine, repository.db.users.schema),
                    )
                    report["result"] = asdict(result)
                    handoff = result.handoff
                    report["passed"] = bool(
                        handoff and (handoff.reference, handoff.arrival_airport, handoff.arrival_date_local,
                                     handoff.arrival_timezone)
                        == ("DEMO-A1", "ZRH", date(2026, 12, 15), "Europe/Zurich")
                        and budget.sequential_lookup_verified(handoff.source_tool_call_id)
                    )
                finally:
                    report["provider_requests"] = budget.attempts
                    report["http_statuses"] = [item.status for item in budget.observations]
                    report["usage"] = budget.usage_totals()
    except TimeoutError:
        report["passed"] = False
        report["error"] = "timeout"
    except Exception as error:
        report["passed"] = False
        report["error"] = type(error).__name__
    write_report(report)
    return 0 if report["passed"] else 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Run the explicitly approved live validation")
    args = parser.parse_args(argv)
    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        return runner.run(run_live_validation() if args.live else run_offline_validation())


if __name__ == "__main__":
    raise SystemExit(main())
