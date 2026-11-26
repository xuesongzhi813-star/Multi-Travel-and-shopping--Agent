from unittest import IsolatedAsyncioTestCase
from uuid import uuid4

from customer_support_chat.app.workflow.hotel_handoff import HotelRoleResult
from customer_support_chat.app.workflow.hotel_history import load_hotel_history
import test_hotel_handoff_graph as handoff_fixture


class HotelHistorySourceTests(IsolatedAsyncioTestCase):
    async def test_replay_sources_are_owned_and_present(self):
        user, thread, reference_message, stay_message = (uuid4() for _ in range(4))
        history = {"request": {"reference": "DEMO-A1", "nights": 1}, "source_messages": {
            "reference": str(reference_message), "nights": str(stay_message),
        }}
        messages = {(user, thread, reference_message): "找 DEMO-A1 的酒店", (user, thread, stay_message): "住 1 晚"}

        async def load(owner, conversation, message):
            return messages.get((owner, conversation, message))

        self.assertEqual(await load_hotel_history(history, user, thread, load), history)
        self.assertIsNone(await load_hotel_history(None, user, thread, load))
        self.assertIsNone(await load_hotel_history(history, uuid4(), thread, load))
        self.assertIsNone(await load_hotel_history(history, user, uuid4(), load))
        malformed = history | {"source_messages": history["source_messages"] | {"nights": "invalid"}}
        self.assertIsNone(await load_hotel_history(malformed, user, thread, load))
        del messages[user, thread, stay_message]
        self.assertIsNone(await load_hotel_history(history, user, thread, load))


class HotelHistoryGraphTests(handoff_fixture.HotelHandoffGraphTests):
    async def hotel(self, user, thread, query):
        self.queries.append((user, thread, query))
        if getattr(self, "simulate_airport_failure", False) and query.area == "airport":
            return HotelRoleResult("unavailable")
        return HotelRoleResult("no_availability")

    async def test_failed_search_preserves_history_and_no_stock_updates_it(self):
        self.simulate_airport_failure = True
        async with self.runtime() as graph:
            first = await self.send(graph, "找 DEMO-A1 的酒店，住 1 晚，市区")
            self.assertEqual(first["status"], "no_availability")
            failed = await self.send(graph, "找 DEMO-A1 的机场酒店，住 2 晚")
            self.assertEqual(failed["status"], "unavailable")
            replay = await self.send(graph, "按上次的酒店条件再查")
            self.assertEqual(replay["status"], "no_availability")
        query = self.queries[-1][2]
        self.assertEqual(query.area, "city")
        self.assertEqual((query.check_out - query.check_in).days, 1)

    async def test_replay_replaces_pending_without_mixing_and_bad_replay_preserves_it(self):
        self.simulate_airport_failure = True
        async with self.runtime() as graph:
            await self.send(graph, "找 DEMO-A1 的酒店，住 1 晚，市区")
            await self.send(graph, "找 DEMO-A1 的机场酒店")
            bad = await self.send(graph, "按上次的酒店条件再查，这次随便")
            self.assertEqual(bad["status"], "needs_clarification")
            finished = await self.send(graph, "住 2 晚")
            self.assertEqual(finished["status"], "unavailable")
            self.assertEqual(self.queries[-1][2].area, "airport")
            await self.send(graph, "找 DEMO-A1 的机场酒店")
            replay = await self.send(graph, "按上次的酒店条件再查")
            self.assertEqual(replay["status"], "no_availability")
            self.assertEqual(self.queries[-1][2].area, "city")
            self.assertEqual((await self.send(graph, "住 2 晚"))["status"], "recorded")
