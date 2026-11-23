import asyncio
from datetime import date
import unittest
from uuid import uuid4

from langchain_core.messages import AIMessage
from pydantic import Field

from customer_support_chat.app.tooling.executor import ToolContext
from customer_support_chat.app.workflow.hotel_handoff import HotelOption, HotelQuery
from flight_role_validation.fixture_model import ScriptedFlightModel, tool_reply
from flight_role_validation.hotel import run_hotel_role


class CapturingModel(ScriptedFlightModel):
    bound_tools: list[str] = Field(default_factory=list)

    def bind_tools(self, tools, **kwargs):
        self.bound_tools[:] = [item.name for item in tools]
        return self


class HotelRoleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.query = HotelQuery("Zürich", "Europe/Zurich", date(2026, 12, 15), date(2026, 12, 17))
        self.option = HotelOption("demo", "Demo", "airport", "standard",
                                  self.query.check_in, self.query.check_out, 37000, "CHF")
        self.context = ToolContext(uuid4(), "hotel", uuid4(), uuid4())
        self.rows = []
        self.lookups = []
        self.options = [self.option]

    async def write(self, record):
        self.rows.append(record)

    async def list_available_hotels(self, query):
        self.lookups.append(query)
        return self.options

    async def invoke(self, model, budget=5):
        return await run_hotel_role(
            self.query, model=model, repository=self, context=self.context,
            audit_sink=self, deadline=asyncio.get_running_loop().time() + budget,
        )

    async def test_model_only_sees_hotel_query_and_tool(self):
        model = CapturingModel(replies=[
            tool_reply("search_hotel_inventory", "search"), AIMessage(content="ignorable"),
        ])
        result = await self.invoke(model)
        self.assertEqual(result.status, "hotel_results")
        self.assertEqual(result.options, (self.option,))
        self.assertEqual(self.lookups, [self.query])
        self.assertEqual(model.bound_tools, ["search_hotel_inventory"])
        prompt = repr(model.received[0])
        self.assertIn("2026-12-15", prompt)
        for excluded in ("DEMO-A1", str(self.context.user_id), "lookup_flight", "lookup_airport_city"):
            self.assertNotIn(excluded, prompt)
        self.assertEqual([(r.tool_call_id, r.status) for r in self.rows], [("search", "ok")])

    async def test_changed_query_is_audited_without_inventory_read(self):
        model = CapturingModel(replies=[
            tool_reply("search_hotel_inventory", "changed", check_in="2026-12-14", room_type="suite"),
        ])
        self.assertEqual((await self.invoke(model)).status, "unavailable")
        self.assertEqual(self.lookups, [])
        self.assertEqual([(r.tool_call_id, r.status) for r in self.rows], [("changed", "invalid_arguments")])

    async def test_timeout_is_distinct_from_empty_inventory(self):
        stalled = CapturingModel(replies=[], delay=1)
        self.assertEqual((await self.invoke(stalled, budget=0.1)).status, "unavailable")
        self.options = []
        empty = CapturingModel(replies=[
            tool_reply("search_hotel_inventory", "empty"), AIMessage(content="empty"),
        ])
        self.assertEqual((await self.invoke(empty)).status, "no_availability")

    async def test_repeated_tool_call_is_rejected(self):
        model = CapturingModel(replies=[
            tool_reply("search_hotel_inventory", "first"), tool_reply("search_hotel_inventory", "second"),
        ])
        self.assertEqual((await self.invoke(model)).status, "unavailable")
        self.assertEqual(self.lookups, [self.query])
        self.assertEqual([r.status for r in self.rows], ["ok", "invalid_arguments"])
