import asyncio
from dataclasses import replace
from datetime import date
from uuid import uuid4
import unittest

from sqlalchemy import inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.schema import DropSchema

from customer_support_chat.app.business.database import create_engine, migrate_business, read_settings
from customer_support_chat.app.business.schema import tables


class BusinessDatabaseTest(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        if hasattr(asyncio, "WindowsSelectorEventLoopPolicy"):
            previous = asyncio.get_event_loop_policy()
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
            cls.addClassCleanup(asyncio.set_event_loop_policy, previous)

    async def asyncSetUp(self):
        self.settings = replace(read_settings(), schema=f"test_p2_{uuid4().hex}")
        self.engine = create_engine(self.settings)
        self.addAsyncCleanup(self.remove_test_schema)
        await migrate_business(self.engine, self.settings.schema)
        self.db = tables(self.settings.schema)

    async def remove_test_schema(self):
        try:
            async with self.engine.begin() as connection:
                await connection.execute(DropSchema(self.settings.schema, cascade=True, if_exists=True))
        finally:
            await self.engine.dispose()


class BusinessMigrationTests(BusinessDatabaseTest):
    async def test_upgrade_is_repeatable_and_schema_is_self_contained(self):
        await migrate_business(self.engine, self.settings.schema)
        async with self.engine.connect() as connection:
            names = await connection.run_sync(
                lambda sync: inspect(sync).get_table_names(schema=self.settings.schema),
            )
        self.assertEqual(set(names), {
            "alembic_version", "users", "auth_sessions", "threads", "messages",
            "flight_itineraries", "hotels", "hotel_inventory", "tool_audit_logs",
            "hotel_booking_proposals", "hotel_bookings", "hotel_booking_receipts", "user_preferences",
        })

    async def test_foreign_keys_and_inventory_constraints(self):
        with self.assertRaises(IntegrityError):
            async with self.engine.begin() as connection:
                await connection.execute(self.db.threads.insert().values(id=uuid4(), user_id=uuid4()))
        async with self.engine.begin() as connection:
            await connection.execute(self.db.hotels.insert().values(
                id="fixture-hotel", name="Fixture hotel", area="airport",
            ))
            row = dict(
                hotel_id="fixture-hotel", stay_date=date(2026, 12, 15), room_type="standard",
                quantity=1, currency="CHF", price_minor=20000,
            )
            await connection.execute(self.db.hotel_inventory.insert().values(**row))
        for changed in ({}, {"quantity": -1}, {"price_minor": -1}):
            values = row | changed
            if changed:
                values["room_type"] = "different"
            with self.subTest(changed=changed), self.assertRaises(IntegrityError):
                async with self.engine.begin() as connection:
                    await connection.execute(self.db.hotel_inventory.insert().values(**values))
        async with self.engine.connect() as connection:
            found = await connection.execute(select(self.db.hotel_inventory.c.quantity))
            self.assertEqual(found.scalars().all(), [1])
