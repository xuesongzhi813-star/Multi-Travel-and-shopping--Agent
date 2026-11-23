from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path

from alembic import command
from alembic.config import Config
from psycopg import AsyncConnection
from psycopg.rows import dict_row
from sqlalchemy import inspect, update

from customer_support_chat.app.business.booking import BookingOwner, BookingScope, BookingSelection, ProposalConflict
from customer_support_chat.app.business.booking_repository import BookingRepository
from customer_support_chat.app.business.database import connection_kwargs, migrate_business
from customer_support_chat.app.business.demo_seed import DEMO_USERS, seed_demo
from customer_support_chat.app.business.repository import BusinessRepository
from customer_support_chat.app.workflow.hotel_handoff import HotelOption
from test_business_migrations import BusinessDatabaseTest
from test_business_seed import PASSWORDS


class BookingRepositoryFixture(BusinessDatabaseTest):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await seed_demo(self.engine, self.settings.schema, PASSWORDS)
        self.booking = BookingRepository(self.settings)
        business = BusinessRepository(self.engine, self.settings.schema)
        user = DEMO_USERS[0][0]
        self.owner = BookingOwner(user, await business.create_thread(user))
        message = await business.append_message(user, self.owner.thread_id, "hotel demo")
        self.selection = BookingSelection(message, 0)
        self.option = HotelOption(
            "demo-airport", "Demo Airport Hotel", "airport", "standard",
            date(2026, 12, 15), date(2026, 12, 16), 18000, "CHF",
        )

    async def create(self, option=None):
        async with await AsyncConnection.connect(**connection_kwargs(self.settings), row_factory=dict_row) as connection:
            return await self.booking.create_proposal(connection, self.owner, self.selection, option or self.option)

class BookingRepositoryTests(BookingRepositoryFixture):
    async def test_proposal_is_owned_snapshotted_and_replayable(self):
        with self.assertRaises(ProposalConflict):
            await self.create(replace(self.option, total_minor=1))
        proposal = await self.create()
        self.assertEqual((proposal.status, proposal.total_minor, proposal.currency), ("awaiting_decision", 18000, "CHF"))
        self.assertEqual((proposal.expires_at - proposal.created_at).total_seconds(), 600)
        self.assertEqual((await self.create()).action_id, proposal.action_id)
        self.assertIsNone(await self.booking.get_proposal(BookingScope(
            replace(self.owner, user_id=DEMO_USERS[1][0]), proposal.action_id,
        )))
        with self.assertRaises(ProposalConflict):
            await self.booking.find_proposal(self.owner, replace(self.selection, option_index=1))
        self.assertNotIn("night_snapshot", proposal.to_json())

    async def test_decision_is_immutable_and_late_decision_cannot_approve(self):
        proposal = await self.create()
        scope = BookingScope(self.owner, proposal.action_id)
        async with self.engine.begin() as connection:
            table = self.db.hotel_booking_proposals
            await connection.execute(update(table).where(table.c.action_id == scope.action_id).values(
                created_at=table.c.created_at - timedelta(minutes=20),
                expires_at=table.c.expires_at - timedelta(minutes=20),
            ))
        expired = await self.booking.record_decision(scope, "approve")
        self.assertEqual(expired.status, "expired")
        self.assertIsNone(expired.decision)

    async def test_first_decision_remains_immutable_on_replay(self):
        proposal = await self.create()
        scope = BookingScope(self.owner, proposal.action_id)
        self.assertEqual((await self.booking.record_decision(scope, "reject")).status, "decided")
        self.assertEqual((await self.booking.record_decision(scope, "reject")).decision, "reject")
        with self.assertRaises(ProposalConflict):
            await self.booking.record_decision(scope, "approve")

    async def test_upgrades_existing_p6_data_and_installs_uniqueness(self):
        def downgrade(connection):
            config = Config(Path(__file__).resolve().parents[1] / "alembic.ini")
            config.attributes.update(connection=connection, business_schema=self.settings.schema)
            command.downgrade(config, "0002_tool_audit")

        async with self.engine.begin() as connection:
            await connection.run_sync(downgrade)
        for _ in range(2):
            await migrate_business(self.engine, self.settings.schema)
        before = await BusinessRepository(self.engine, self.settings.schema).load_thread(
            self.owner.user_id, self.owner.thread_id,
        )
        self.assertEqual(before["messages"][0]["message_id"], self.selection.message_id)
        self.assertEqual((await self.create()).status, "awaiting_decision")
        async with self.engine.connect() as connection:
            schema = self.settings.schema
            indexes = await connection.run_sync(
                lambda conn: inspect(conn).get_indexes("hotel_booking_proposals", schema=schema),
            )
            constraints = await connection.run_sync(
                lambda conn: inspect(conn).get_unique_constraints("hotel_bookings", schema=schema),
            )
        active = next(index for index in indexes if index["name"] == "hotel_proposal_pending_thread_uq")
        self.assertTrue(active["unique"])
        self.assertIn("awaiting_decision", active["dialect_options"]["postgresql_where"])
        self.assertIn(["action_id"], [constraint["column_names"] for constraint in constraints])
