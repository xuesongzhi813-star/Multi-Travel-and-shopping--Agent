import asyncio
from datetime import date
from uuid import uuid4

from alembic import command
from alembic.config import Config
from psycopg import errors
from sqlalchemy import inspect, select

from customer_support_chat.app.business.database import ROOT, migrate_business
from customer_support_chat.app.business.preference_repository import PreferenceRepository
from test_business_migrations import BusinessDatabaseTest


EMPTY = {"room_type": None, "area": None, "max_total_chf_minor": None}


class PreferenceRepositoryTests(BusinessDatabaseTest):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.repository = PreferenceRepository(self.settings)
        self.a, self.b = uuid4(), uuid4()
        async with self.engine.begin() as connection:
            await connection.execute(self.db.users.insert(), [
                {"id": self.a, "username": "a", "password_hash": "unused"},
                {"id": self.b, "username": "b", "password_hash": "unused"},
            ])

    async def test_upgrade_from_p9_preserves_data(self):
        def downgrade(connection):
            config = Config(ROOT / "alembic.ini")
            config.attributes.update(connection=connection, business_schema=self.settings.schema)
            command.downgrade(config, "0004_hotel_catalog")

        thread, message = uuid4(), uuid4()
        async with self.engine.begin() as connection:
            await connection.run_sync(downgrade)
            await connection.execute(self.db.threads.insert().values(id=thread, user_id=self.a))
            await connection.execute(self.db.messages.insert().values(id=message, thread_id=thread, content="既有消息"))
            await connection.execute(self.db.hotels.insert().values(id="fixture", name="Fixture", area="city"))
            await connection.execute(self.db.hotel_inventory.insert().values(
                hotel_id="fixture", stay_date=date(2026, 12, 15), room_type="standard",
                quantity=2, price_minor=12000, currency="CHF",
            ))
        for _ in range(2):
            await migrate_business(self.engine, self.settings.schema)
        async with self.engine.connect() as connection:
            self.assertTrue(await connection.run_sync(
                lambda sync: inspect(sync).has_table("user_preferences", schema=self.settings.schema),
            ))
            self.assertEqual(await connection.scalar(select(self.db.users.c.username).where(self.db.users.c.id == self.a)), "a")
            self.assertEqual(await connection.scalar(select(self.db.messages.c.content)), "既有消息")
            self.assertEqual(await connection.scalar(select(self.db.hotel_inventory.c.quantity)), 2)

    async def test_database_constraints_and_empty_projection(self):
        self.assertEqual(await self.repository.get(self.a), EMPTY)
        with self.assertRaises(errors.ForeignKeyViolation):
            await self.repository.patch(uuid4(), {"area": "city"})
        for invalid in ({"room_type": "suite"}, {"area": "suburb"},
                        {"max_total_chf_minor": 0}, {"max_total_chf_minor": 1000001}):
            with self.subTest(invalid=invalid), self.assertRaises(errors.CheckViolation):
                await self.repository.patch(self.a, invalid)
        self.assertEqual(await self.repository.get(self.a), EMPTY)

    async def test_concurrent_first_patch_preserves_disjoint_fields(self):
        await asyncio.gather(
            self.repository.patch(self.a, {"area": "city"}),
            self.repository.patch(self.a, {"max_total_chf_minor": 40000}),
        )
        self.assertEqual(await self.repository.get(self.a), EMPTY | {"area": "city", "max_total_chf_minor": 40000})
        self.assertEqual(await self.repository.get(self.b), EMPTY)

    async def test_clear_then_delete_twice(self):
        await self.repository.patch(self.a, {"room_type": "standard", "area": "airport"})
        self.assertEqual(await self.repository.patch(self.a, {"area": None}), EMPTY | {"room_type": "standard"})
        await self.repository.delete(self.a)
        await self.repository.delete(self.a)
        self.assertEqual(await self.repository.get(self.a), EMPTY)
