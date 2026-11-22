import asyncio
from datetime import date
import unittest
from unittest.mock import patch

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from customer_support_chat.app.business.auth import hash_password, verify_password
from customer_support_chat.app.business.demo_seed import seed_demo
from test_business_migrations import BusinessDatabaseTest


PASSWORDS = ("synthetic-fixture-password-a", "synthetic-fixture-password-b")


class PasswordTests(unittest.TestCase):
    def test_password_hash_is_salted_and_validates(self):
        first = hash_password(PASSWORDS[0])
        second = hash_password(PASSWORDS[0])
        self.assertNotEqual(first, second)
        self.assertNotIn(PASSWORDS[0], first)
        self.assertTrue(verify_password(PASSWORDS[0], first))
        self.assertFalse(verify_password(PASSWORDS[1], first))
        self.assertFalse(verify_password(PASSWORDS[0], "broken"))


class BusinessSeedTests(BusinessDatabaseTest):
    async def counts(self):
        async with self.engine.connect() as connection:
            return [
                await connection.scalar(select(func.count()).select_from(table))
                for table in (self.db.users, self.db.flight_itineraries, self.db.hotel_inventory)
            ]

    async def test_rerun_and_concurrent_seed_preserve_inventory_change(self):
        await asyncio.gather(
            seed_demo(self.engine, self.settings.schema, PASSWORDS),
            seed_demo(self.engine, self.settings.schema, PASSWORDS),
        )
        self.assertEqual(await self.counts(), [2, 6, 2])
        airport = self.db.hotel_inventory.c.hotel_id == "demo-airport"
        async with self.engine.begin() as connection:
            await connection.execute(self.db.hotel_inventory.update().where(airport).values(quantity=1))
        await seed_demo(self.engine, self.settings.schema, ("alternate-a-password", "alternate-b-password"))
        self.assertEqual(await self.counts(), [2, 6, 2])
        async with self.engine.connect() as connection:
            quantity = await connection.scalar(select(self.db.hotel_inventory.c.quantity).where(airport))
            quantities = (await connection.scalars(select(self.db.hotel_inventory.c.quantity))).all()
            password_hash = await connection.scalar(
                select(self.db.users.c.password_hash).where(self.db.users.c.username == "demo-a"),
            )
        self.assertEqual(quantity, 1)
        self.assertIn(0, quantities)
        self.assertTrue(verify_password(PASSWORDS[0], password_hash))

    async def test_database_error_rolls_back_all_seed_inserts(self):
        invalid = {
            "hotel_id": "demo-airport", "stay_date": date(2026, 12, 15),
            "room_type": "standard", "quantity": -1, "price_minor": 10000, "currency": "CHF",
        }
        with patch("customer_support_chat.app.business.demo_seed.INVENTORY", [invalid]):
            with self.assertRaises(IntegrityError):
                await seed_demo(self.engine, self.settings.schema, PASSWORDS)
        self.assertEqual(await self.counts(), [0, 0, 0])
