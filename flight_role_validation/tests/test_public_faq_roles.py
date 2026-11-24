import asyncio
from datetime import date, datetime, timezone
import unittest
from unittest.mock import AsyncMock
from uuid import uuid4

from langchain_core.messages import AIMessage

from customer_support_chat.app.tooling.executor import ToolContext, ToolExecutor, ToolRegistry
from customer_support_chat.app.workflow.hotel_handoff import HotelQuery
from faq_extension.contracts import FaqSearchResult, PublicFaqQuestion
from flight_role_validation.executor import FlightFacts
from flight_role_validation.fixture_model import ScriptedFlightModel, tool_reply
from flight_role_validation.hotel import run_hotel_role
from flight_role_validation.public_faq import FAQ_TOOL, register_public_faq
from flight_role_validation.role import FlightRoleInput, InMemoryAudit, run_flight_role


QUESTION = PublicFaqQuestion(text="行李延误", category="baggage")


class PublicFaqRoleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.user, self.thread, self.run_id = uuid4(), uuid4(), uuid4()
        self.audit = InMemoryAudit()
        self.public_search = AsyncMock(return_value=FaqSearchResult(status="no_evidence"))

    async def list_available_hotels(self, query):
        return []

    async def flight_lookup(self, user, reference):
        return FlightFacts(reference, "ZRH", datetime(2026, 12, 15, tzinfo=timezone.utc), "Europe/Zurich")

    async def test_script_flight_and_hotel_call_faq_once_without_losing_business_receipt(self):
        flight_model = ScriptedFlightModel(replies=[
            tool_reply("lookup_flight", "flight-read", reference="DEMO-A1"),
            tool_reply(FAQ_TOOL, "flight-policy"),
            tool_reply("HandoffCandidate", "finish", reference="DEMO-A1"),
        ])
        flight = await run_flight_role(
            FlightRoleInput(self.user, "Look up designated flight.", "DEMO-A1"),
            model=flight_model, lookup=self.flight_lookup,
            faq_question=QUESTION, faq_lookup=self.public_search, audit_sink=self.audit,
        )
        self.assertEqual(flight.handoff.reference, "DEMO-A1")
        self.assertEqual(flight.faq.status, "no_evidence")
        hotel_model = ScriptedFlightModel(replies=[
            tool_reply("search_hotel_inventory", "stock"), tool_reply(FAQ_TOOL, "hotel-policy"),
            AIMessage(content="Done"),
        ])
        hotel = await self.hotel(hotel_model)
        self.assertEqual(hotel.status, "no_availability")
        self.assertEqual(hotel.faq.status, "no_evidence")
        self.assertEqual(self.public_search.await_count, 2)
        rows = [row for row in self.audit.records if row.tool_name == FAQ_TOOL]
        self.assertEqual([(row.arguments, row.result_summary) for row in rows],
                         [({}, {"status": "no_evidence", "degradation": None})] * 2)

    async def hotel(self, model):
        return await run_hotel_role(
            HotelQuery("Zürich", "Europe/Zurich", date(2026, 12, 15), date(2026, 12, 16)),
            model=model, repository=self, context=ToolContext(self.user, "hotel", self.run_id, self.thread),
            audit_sink=self.audit, deadline=asyncio.get_running_loop().time() + 8,
            faq_question=QUESTION, faq_lookup=self.public_search,
        )

    async def test_wrong_role_and_second_call_do_not_reach_provider(self):
        registry = ToolRegistry()
        register_public_faq(registry, self.public_search)
        result = await ToolExecutor(registry, self.audit).execute(
            ToolContext(self.user, "airport", self.run_id), FAQ_TOOL, QUESTION.model_dump(), "bad-role",
        )
        self.assertEqual(result.status, "permission_denied")
        model = ScriptedFlightModel(replies=[
            tool_reply("search_hotel_inventory", "stock"),
            tool_reply(FAQ_TOOL, "once"),
            tool_reply(FAQ_TOOL, "twice"),
        ])
        await self.hotel(model)
        self.public_search.assert_awaited_once_with(QUESTION)
        self.assertEqual(self.audit.records[-1].status, "invalid_arguments")

    async def test_faq_failure_preserves_verified_business_result(self):
        self.public_search.side_effect = TimeoutError("provider timeout")
        result = await self.hotel(ScriptedFlightModel(replies=[
            tool_reply("search_hotel_inventory", "stock"), tool_reply(FAQ_TOOL, "policy"), AIMessage(content="Done"),
        ]))
        self.assertEqual(result.status, "no_availability")
        self.assertEqual(result.faq.status, "unavailable")
