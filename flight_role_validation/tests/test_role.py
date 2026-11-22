import asyncio
from datetime import date, datetime, timezone
import unittest
from uuid import UUID

from langchain_core.messages import ToolMessage

from flight_role_validation.executor import FlightFacts
from flight_role_validation.fixture_model import ScriptedFlightModel, synthetic_replies, tool_reply
from flight_role_validation.role import FlightRoleInput, run_flight_role


USER = UUID(int=1)


class FlightRoleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.lookups = []

    async def lookup(self, user_id, reference):
        self.assertEqual(user_id, USER)
        self.lookups.append(reference)
        if reference in {"DEMO-A1", "DEMO-A2"}:
            return FlightFacts(reference, "ZRH", datetime(2026, 12, 14, 23, 30, tzinfo=timezone.utc),
                               "Europe/Zurich")
        return None

    async def run_role(self, model, *, target="DEMO-A1", timeout=40):
        return await run_flight_role(
            FlightRoleInput(USER, "Use the designated flight.", target),
            model=model, lookup=self.lookup, timeout=timeout,
        )

    async def test_two_tool_rounds_return_verified_midnight_handoff(self):
        model = ScriptedFlightModel(replies=synthetic_replies())
        result = await self.run_role(model)
        self.assertEqual(result.status, "verified")
        self.assertEqual(len(model.received), 3)
        self.assertEqual(self.lookups, ["DEMO-A1", "DEMO-A2"])
        for index, call_id in ((1, "lookup-a1"), (2, "lookup-a2")):
            self.assertIsInstance(model.received[index][-1], ToolMessage)
            self.assertEqual(model.received[index][-1].tool_call_id, call_id)
        handoff = result.handoff
        self.assertEqual((handoff.reference, handoff.arrival_airport, handoff.arrival_date_local,
                          handoff.arrival_timezone, handoff.source_tool_call_id),
                         ("DEMO-A1", "ZRH", date(2026, 12, 15), "Europe/Zurich", "lookup-a1"))

    async def test_missing_target_skips_model(self):
        model = ScriptedFlightModel(replies=[])
        self.assertEqual((await self.run_role(model, target=None)).status, "needs_clarification")
        self.assertEqual(model.received, [])

    async def test_wrong_or_unqueried_target_cannot_handoff(self):
        for reference, tool_reference in (("DEMO-A2", "DEMO-A2"), ("DEMO-A1", "DEMO-A2"),
                                          ("DEMO-B1", "DEMO-B1")):
            with self.subTest(reference=reference, tool_reference=tool_reference):
                model = ScriptedFlightModel(replies=[
                    tool_reply("lookup_flight", "actual-lookup", reference=tool_reference),
                    tool_reply("HandoffCandidate", "structured", reference=reference),
                ])
                result = await self.run_role(model)
                self.assertEqual((result.status, result.reason), ("unavailable", "unverified_evidence"))
                self.assertIsNone(result.handoff)

    async def test_bad_model_tool_inputs_never_reach_lookup(self):
        for proposal in (tool_reply("write_booking", "bad", reference="DEMO-A1"),
                         tool_reply("lookup_flight", "bad", reference="invalid"),
                         tool_reply("lookup_flight", "bad", reference="DEMO-A1", user_id=str(UUID(int=2))),
                         tool_reply("HandoffCandidate", "bad", reference="DEMO-A1", source_tool_call_id="fake")):
            with self.subTest(tool=proposal.tool_calls):
                model = ScriptedFlightModel(replies=[proposal])
                result = await self.run_role(model)
                self.assertEqual((result.status, result.reason), ("unavailable", "protocol_error"))
        self.assertEqual(self.lookups, [])

    async def test_timeout_and_expired_budget_terminate(self):
        stopped = ScriptedFlightModel(replies=[], delay=0.2)
        result = await self.run_role(stopped, timeout=0.01)
        self.assertEqual((result.status, result.reason), ("unavailable", "timeout"))

        attempts = [tool_reply("lookup_flight", f"lookup-{index}", reference="DEMO-A1") for index in range(4)]
        model = ScriptedFlightModel(replies=attempts)
        result = await self.run_role(model)
        self.assertEqual((result.status, result.reason), ("unavailable", "budget_exhausted"))
        self.assertEqual(len(model.received), 3)
        self.assertEqual(len(self.lookups), 2)
