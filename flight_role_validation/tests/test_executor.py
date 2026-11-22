"""Verify the P4 lookup's ownership predicate against the synthetic business store."""

import asyncio
from dataclasses import replace
import unittest
from uuid import uuid4

from sqlalchemy.schema import DropSchema

from customer_support_chat.app.business.database import create_engine, migrate_business, read_settings
from customer_support_chat.app.business.demo_seed import DEMO_USERS, seed_demo
from customer_support_chat.app.business.repository import BusinessRepository
from flight_role_validation.executor import FlightExecutor, build_repository_lookup


class FlightExecutorTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        if hasattr(asyncio, "WindowsSelectorEventLoopPolicy"):
            previous = asyncio.get_event_loop_policy()
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
            cls.addClassCleanup(asyncio.set_event_loop_policy, previous)

    async def test_executor_owner_and_not_found(self):
        settings = replace(read_settings(), schema=f"p4_test_{uuid4().hex}")
        engine = create_engine(settings)
        self.addAsyncCleanup(engine.dispose)

        async def drop_schema():
            async with engine.begin() as connection:
                await connection.execute(DropSchema(settings.schema, cascade=True, if_exists=True))

        self.addAsyncCleanup(drop_schema)
        await migrate_business(engine, settings.schema)
        await seed_demo(engine, settings.schema, ("p4-test-demo-a-01", "p4-test-demo-b-02"))
        lookup = build_repository_lookup(BusinessRepository(engine, settings.schema))

        executor = FlightExecutor(DEMO_USERS[0][0], lookup)
        flight = await executor.lookup_flight("DEMO-A1", "call-owned")
        self.assertEqual((flight.reference, flight.arrival_airport, flight.arrival_timezone),
                         ("DEMO-A1", "ZRH", "Europe/Zurich"))
        self.assertIsNone(await executor.lookup_flight("DEMO-B1", "call-foreign"))
        self.assertIsNone(await executor.lookup_flight("DEMO-A9", "call-missing"))
        self.assertIsNone(executor.evidence("DEMO-B1"))
        self.assertIsNone(executor.evidence("DEMO-A9"))

    async def test_invalid_reference_skips_repository(self):
        async def unexpected_lookup(*args):
            self.fail("invalid reference reached repository")

        executor = FlightExecutor(DEMO_USERS[0][0], unexpected_lookup)
        for bad in ("", "DEMO-Ａ1", "demo-a1", "DEMO-A1234", "DEMO-A1 "):
            with self.subTest(reference=bad), self.assertRaises(ValueError):
                await executor.lookup_flight(bad, "call-invalid")

    async def test_evidence_records_only_successful_same_turn(self):
        from datetime import datetime, timezone
        from flight_role_validation.executor import FlightFacts

        user = DEMO_USERS[0][0]
        async def lookup(user_id, reference):
            self.assertEqual(user_id, user)
            return FlightFacts(reference, "ZRH", datetime(2026, 12, 14, 23, 30, tzinfo=timezone.utc), "Europe/Zurich")

        first = FlightExecutor(user, lookup)
        other = FlightExecutor(user, lookup)
        facts = await first.lookup_flight("DEMO-A1", "real-tool-id")
        evidence = first.evidence("DEMO-A1")
        self.assertEqual((evidence.facts, evidence.tool_call_id), (facts, "real-tool-id"))
        self.assertIsNone(other.evidence("DEMO-A1"))
