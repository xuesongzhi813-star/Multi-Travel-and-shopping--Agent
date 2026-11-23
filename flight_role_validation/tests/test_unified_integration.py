import asyncio
from datetime import date
import unittest
from uuid import uuid4

from sqlalchemy import select

from customer_support_chat.app.business.demo_seed import DEMO_USERS
from customer_support_chat.app.business.schema import tables
from customer_support_chat.app.tooling.audit import PostgresAuditSink
from customer_support_chat.app.tooling.executor import ToolContext, ToolExecutor, ToolRegistry
from flight_role_validation.__main__ import SYNTHETIC_INPUT, synthetic_lookup, temporary_repository
from flight_role_validation.executor import build_repository_lookup, register_flight_tool
from flight_role_validation.fixture_model import ScriptedFlightModel, synthetic_replies, tool_reply
from flight_role_validation.mcp_tools import AIRPORT_TOOL, MCP_ROLE, open_airport_session, register_airport_tools
from flight_role_validation.role import run_flight_role


class UnifiedIntegrationTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        if hasattr(asyncio, "WindowsSelectorEventLoopPolicy"):
            old = asyncio.get_event_loop_policy()
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
            cls.addClassCleanup(asyncio.set_event_loop_policy, old)

    async def test_flight_agent_preserves_handoff_and_audits_rejections(self):
        async with temporary_repository(asyncio.get_running_loop().time() + 30) as repo:
            sink = PostgresAuditSink(repo.engine, repo.db.users.schema)
            context = ToolContext(DEMO_USERS[0][0], "flight", uuid4())
            success = await run_flight_role(
                SYNTHETIC_INPUT, model=ScriptedFlightModel(replies=synthetic_replies()),
                lookup=build_repository_lookup(repo), context=context, audit_sink=sink,
            )
            self.assertEqual(success.status, "verified")
            self.assertEqual((success.handoff.reference, success.handoff.arrival_date_local),
                             ("DEMO-A1", date(2026, 12, 15)))
            for identifier, tool, args in (
                ("bad-args", "lookup_flight", {"reference": "DEMO-A1", "user_id": "private"}),
                ("bad-tool", "unauthorized", {"private": "secret text"}),
            ):
                outcome = await run_flight_role(
                    SYNTHETIC_INPUT, model=ScriptedFlightModel(replies=[tool_reply(tool, identifier, **args)]),
                    lookup=synthetic_lookup, context=context, audit_sink=sink,
                )
                self.assertEqual((outcome.status, outcome.reason), ("unavailable", "protocol_error"))

            over_limit = ScriptedFlightModel(replies=[
                tool_reply("lookup_flight", f"limit-{index}", reference="DEMO-A1") for index in range(3)
            ])
            capped = await run_flight_role(
                SYNTHETIC_INPUT, model=over_limit, lookup=synthetic_lookup, context=context, audit_sink=sink,
            )
            self.assertEqual((capped.status, capped.reason), ("unavailable", "budget_exhausted"))
            audit = tables(repo.db.users.schema).tool_audit_logs
            async with repo.engine.connect() as conn:
                rows = {row["tool_call_id"]: row for row in (await conn.execute(select(audit))).mappings()}
            self.assertEqual({k: row["status"] for k, row in rows.items()}, {
                "lookup-a1": "ok", "lookup-a2": "ok", "bad-args": "invalid_arguments",
                "bad-tool": "permission_denied", "limit-0": "ok", "limit-1": "ok",
            })
            self.assertNotIn("DEMO-A1", repr(rows))
            self.assertNotIn("secret text", repr(rows))

    async def test_one_executor_runs_builtin_and_mcp_with_real_audit(self):
        async with temporary_repository(asyncio.get_running_loop().time() + 30) as repo:
            async with open_airport_session() as session:
                registry = ToolRegistry()
                register_flight_tool(registry, build_repository_lookup(repo))
                self.assertTrue(await register_airport_tools(registry, session))
                sink = PostgresAuditSink(repo.engine, repo.db.users.schema)
                executor = ToolExecutor(registry, sink)
                run_id = uuid4()
                flight_context = ToolContext(DEMO_USERS[0][0], "flight", run_id)
                airport_context = ToolContext(DEMO_USERS[0][0], MCP_ROLE, run_id)
                self.assertEqual([item["name"] for item in registry.list_for_role("flight")], ["lookup_flight"])
                flight = await executor.execute(flight_context, "lookup_flight", {"reference": "DEMO-A1"}, "flight")
                city = await executor.execute(airport_context, AIRPORT_TOOL, {"code": "ZRH"}, "airport")
                denied = await executor.execute(flight_context, AIRPORT_TOOL, {"code": "ZRH"}, "denied")
                self.assertEqual((flight.status, city.status, denied.status), ("ok", "ok", "permission_denied"))
                self.assertEqual((flight.data["arrival_airport"], city.data["city"]), ("ZRH", "Zürich"))

            audit = tables(repo.db.users.schema).tool_audit_logs
            async with repo.engine.connect() as conn:
                rows = {row["tool_call_id"]: row for row in (await conn.execute(select(audit))).mappings()}
            self.assertEqual(set(rows), {"flight", "airport", "denied"})
            self.assertEqual(rows["flight"]["arguments"], {})
            self.assertEqual(rows["airport"]["arguments"], {"code": "ZRH"})
            self.assertEqual(rows["denied"]["arguments"], {})

    async def test_grouped_and_malformed_business_calls_are_audited(self):
        from langchain_core.messages import AIMessage

        class Sink:
            def __init__(self):
                self.rows = []

            async def write(self, row):
                self.rows.append(row)

        async def never_lookup(*args):
            self.fail("unsafe response invoked business lookup")

        sink = Sink()
        grouped = AIMessage(content="", tool_calls=[
            {"name": "unapproved", "id": "u1", "args": {}},
            {"name": "lookup_flight", "id": "f1", "args": {"reference": "DEMO-A1"}},
        ])
        malformed = AIMessage(content="", invalid_tool_calls=[
            {"name": "lookup_flight", "id": "m1", "args": "not JSON", "error": "private"},
        ])
        for message in (grouped, malformed):
            result = await run_flight_role(
                SYNTHETIC_INPUT, model=ScriptedFlightModel(replies=[message]),
                lookup=never_lookup, audit_sink=sink,
            )
            self.assertEqual((result.status, result.reason), ("unavailable", "protocol_error"))
        self.assertEqual({row.tool_call_id: row.status for row in sink.rows}, {
            "u1": "permission_denied", "f1": "invalid_arguments", "m1": "invalid_arguments",
        })
        self.assertNotIn("not JSON", repr(sink.rows))
