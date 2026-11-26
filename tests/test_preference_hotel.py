from uuid import uuid4

from customer_support_chat.app.business.preference_repository import PreferenceRepository
from customer_support_chat.app.workflow.checkpoints import open_graph_runtime
import test_hotel_handoff_graph as handoff_fixture


class PreferenceHotelTests(handoff_fixture.HotelHandoffGraphTests):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        async with self.engine.begin() as connection:
            await connection.execute(self.db.users.insert().values(id=self.user, username="fixture", password_hash="unused"))
        self.preferences = PreferenceRepository(self.settings)

    def runtime(self):
        return open_graph_runtime(
            self.settings, self.schema, self.load, flight_lookup=self.flight, hotel_search=self.hotel,
            preference_repository=self.preferences,
        )

    async def test_new_thread_uses_live_defaults_and_explicit_fields_win(self):
        stored = {"area": "city", "room_type": "standard", "max_total_chf_minor": 40000}
        await self.preferences.patch(self.user, stored)
        async with self.runtime() as graph:
            first = await self.send(graph, "找 DEMO-A1 的酒店，住 1 晚", thread=uuid4())
            self.assertEqual(first["status"], "no_availability")
            override = await self.send(graph, "找 DEMO-A1 的机场酒店，住 1 晚，总预算 300 瑞郎", thread=uuid4())
            self.assertEqual(override["status"], "no_availability")
        filters = [(query.area, query.room_type, query.max_total_chf_minor) for _, _, query in self.queries]
        self.assertEqual(filters, [("city", "standard", 40000), ("airport", "standard", 30000)])
        self.assertEqual(await self.preferences.get(self.user), stored)

    async def test_pref_changes_during_stay_clarification_are_applied_late(self):
        await self.preferences.patch(self.user, {"area": "city", "max_total_chf_minor": 40000})
        async with self.runtime() as graph:
            self.assertEqual((await self.send(graph, "找 DEMO-A1 的酒店"))["status"], "needs_clarification")
            await self.preferences.patch(self.user, {"max_total_chf_minor": 32000, "area": None})
            self.assertEqual((await self.send(graph, "住 2 晚，机场附近"))["status"], "no_availability")
        query = self.queries[-1][2]
        self.assertEqual((query.area, query.max_total_chf_minor), ("airport", 32000))

    async def test_invalid_trip_budget_does_not_fall_back_to_default(self):
        await self.preferences.patch(self.user, {"max_total_chf_minor": 40000})
        async with self.runtime() as graph:
            answer = await self.send(graph, "找 DEMO-A1 的酒店，住 1 晚，预算 300")
            self.assertEqual(answer["status"], "needs_clarification")
            self.assertIn("预算", answer["reply"])
        self.assertEqual(self.queries, [])

    async def test_whole_stay_total_price_limit_overrides_saved_budget(self):
        await self.preferences.patch(self.user, {"max_total_chf_minor": 40000})
        async with self.runtime() as graph:
            await self.send(graph, "找 DEMO-A1 的机场酒店，住 1 晚，整段总价不超过 100 瑞郎")
        self.assertEqual(self.queries[-1][2].max_total_chf_minor, 10000)

    async def test_budget_clarification_resumes_the_new_query(self):
        async with self.runtime() as graph:
            await self.send(graph, "找 DEMO-A1 的机场酒店，住 1 晚，预算 200")
            reply = await self.send(graph, "找 DEMO-A1 的市区酒店，住 2 晚，预算 300")
            self.assertEqual(reply["status"], "needs_clarification")
            self.assertEqual(self.queries, [])
            result = await self.send(graph, "总预算 300 瑞郎")
            self.assertEqual(result["status"], "no_availability")
        query = self.queries[-1][2]
        self.assertEqual((query.area, query.max_total_chf_minor, (query.check_out - query.check_in).days),
                         ("city", 30000, 2))

    async def test_repeating_same_flight_keeps_explicit_pending_filters(self):
        await self.preferences.patch(self.user, {"area": "airport", "max_total_chf_minor": 40000})
        async with self.runtime() as graph:
            await self.send(graph, "找 DEMO-A1 的市区酒店，总预算 300 瑞郎")
            await self.send(graph, "DEMO-A1，住 2 晚")
        query = self.queries[-1][2]
        self.assertEqual((query.area, query.max_total_chf_minor), ("city", 30000))
