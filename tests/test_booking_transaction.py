import asyncio
from dataclasses import replace
from datetime import timedelta
from uuid import uuid4

from psycopg import Error
from sqlalchemy import func, select, text, update

from customer_support_chat.app.business.booking import BookingOwner, BookingScope, BookingSelection, ProposalConflict, ProposalNotFound
from customer_support_chat.app.business.demo_seed import DEMO_USERS
from customer_support_chat.app.business.repository import BusinessRepository
from customer_support_chat.app.tooling.audit import PostgresAuditSink
from customer_support_chat.app.tooling.booking import ApprovedBookingExecutor
from test_booking_repository import BookingRepositoryFixture


class BookingTransactionTests(BookingRepositoryFixture):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.add_night()
        self.option = replace(self.option, check_out=self.option.check_out + timedelta(days=1), total_minor=37000)

    async def add_night(self):
        async with self.engine.begin() as connection:
            await connection.execute(self.db.hotel_inventory.insert().values(
                hotel_id="demo-airport", room_type="standard", stay_date=self.option.check_out,
                quantity=3, price_minor=19000, currency="CHF",
            ))

    async def snapshot(self):
        inventory = self.db.hotel_inventory
        async with self.engine.connect() as connection:
            quantities = (await connection.execute(select(inventory.c.quantity).where(
                inventory.c.hotel_id == "demo-airport",
            ).order_by(inventory.c.stay_date))).scalars().all()
            orders = await connection.scalar(select(func.count()).select_from(self.db.hotel_bookings))
            receipts = await connection.scalar(select(func.count()).select_from(self.db.hotel_booking_receipts))
        return quantities, orders, receipts

    async def approve(self):
        proposal = await self.create()
        scope = BookingScope(self.owner, proposal.action_id)
        await self.booking.record_decision(scope, "approve")
        return scope

    async def expire(self, scope):
        table = self.db.hotel_booking_proposals
        async with self.engine.begin() as connection:
            await connection.execute(update(table).where(table.c.action_id == scope.action_id).values(
                created_at=table.c.created_at - timedelta(minutes=20), expires_at=table.c.expires_at - timedelta(minutes=20),
            ))

    async def test_approved_booking_is_atomic_and_replay_returns_same_order(self):
        scope = await self.approve()
        one, two = await asyncio.gather(self.booking.resolve_action(scope), self.booking.resolve_action(scope))
        self.assertEqual(one.proposal.status, "booked")
        self.assertEqual(one.proposal.booking_id, two.proposal.booking_id)
        self.assertIsNotNone(one.proposal.booking_id)
        self.assertEqual(sorted((one.reused_receipt, two.reused_receipt)), [False, True])
        self.assertEqual(await self.snapshot(), ([2, 2], 1, 1))

    async def test_unapproved_rejected_expired_or_wrong_owner_never_decrements(self):
        proposal = await self.create()
        scope = BookingScope(self.owner, proposal.action_id)
        with self.assertRaises(ProposalConflict):
            await self.booking.resolve_action(scope)
        with self.assertRaises(ProposalNotFound):
            await self.booking.resolve_action(BookingScope(replace(self.owner, user_id=DEMO_USERS[1][0]), scope.action_id))
        await self.booking.record_decision(scope, "reject")
        self.assertEqual((await self.booking.resolve_action(scope)).proposal.status, "rejected")
        self.assertEqual(await self.snapshot(), ([3, 3], 0, 0))

    async def test_competing_threads_and_changed_night_quote_are_stale(self):
        inventory = self.db.hotel_inventory
        async with self.engine.begin() as connection:
            await connection.execute(update(inventory).where(inventory.c.hotel_id == "demo-airport").values(quantity=1))
        first = await self.approve()
        business = BusinessRepository(self.engine, self.settings.schema)
        user = DEMO_USERS[1][0]
        self.owner = BookingOwner(user, await business.create_thread(user))
        self.selection = BookingSelection(await business.append_message(user, self.owner.thread_id, "demo"), 0)
        second = await self.approve()
        outcomes = await asyncio.gather(self.booking.resolve_action(first), self.booking.resolve_action(second))
        self.assertEqual(sorted(item.proposal.status for item in outcomes), ["booked", "stale"])
        self.assertEqual(await self.snapshot(), ([0, 0], 1, 1))

    async def test_equal_total_with_changed_nightly_prices_is_stale(self):
        scope = await self.approve()
        inventory = self.db.hotel_inventory
        async with self.engine.begin() as connection:
            for day, changed in ((self.option.check_in, 18100), (self.option.check_in + timedelta(days=1), 18900)):
                await connection.execute(update(inventory).where(
                    inventory.c.hotel_id == "demo-airport", inventory.c.stay_date == day,
                ).values(price_minor=changed))
        self.assertEqual((await self.booking.resolve_action(scope)).proposal.status, "stale")
        self.assertEqual(await self.snapshot(), ([3, 3], 0, 0))

    async def test_failed_receipt_insert_rolls_back_booking_and_all_stock(self):
        scope = await self.approve()
        schema = self.settings.schema
        async with self.engine.begin() as connection:
            await connection.execute(text(f"""
                CREATE FUNCTION {schema}.fail_receipt() RETURNS trigger LANGUAGE plpgsql AS $$
                BEGIN RAISE EXCEPTION 'test rollback'; END; $$;
            """))
            await connection.execute(text(f"""
                CREATE TRIGGER fail_receipt BEFORE INSERT ON {schema}.hotel_booking_receipts
                FOR EACH ROW EXECUTE FUNCTION {schema}.fail_receipt()
            """))
        with self.assertRaises(Error):
            await self.booking.resolve_action(scope)
        self.assertEqual(await self.snapshot(), ([3, 3], 0, 0))
        async with self.engine.begin() as connection:
            await connection.execute(text(f"DROP TRIGGER fail_receipt ON {schema}.hotel_booking_receipts"))
        self.assertEqual((await self.booking.resolve_action(scope)).proposal.status, "booked")

    async def test_existing_receipt_outlives_expiry_but_unexecuted_approval_does_not(self):
        scope = await self.approve()
        await self.expire(scope)
        self.assertEqual((await self.booking.resolve_action(scope)).proposal.status, "expired")
        business = BusinessRepository(self.engine, self.settings.schema)
        self.selection = BookingSelection(await business.append_message(
            self.owner.user_id, self.owner.thread_id, "another demo"),
            0,
        )
        scope = await self.approve()
        booked = await self.booking.resolve_action(scope)
        await self.expire(scope)
        self.assertEqual((await self.booking.resolve_action(scope)).proposal.booking_id, booked.proposal.booking_id)

    async def test_audit_correlates_action_and_failure_keeps_receipt(self):
        class FailedAudit:
            async def write(self, record):
                raise RuntimeError("mock audit failure")

        scope = await self.approve()
        request = uuid4()
        audit = PostgresAuditSink(self.engine, self.settings.schema)
        booked = await ApprovedBookingExecutor(self.booking, audit).execute(scope, request_id=request)
        async with self.engine.connect() as connection:
            row = (await connection.execute(select(self.db.tool_audit_logs))).mappings().one()
        self.assertEqual((row["run_id"], row["action_id"], row["tool_name"]), (request, scope.action_id, "book_hotel"))
        self.assertEqual(row["arguments"], {})
        replay = await ApprovedBookingExecutor(self.booking, FailedAudit()).execute(scope, request_id=uuid4())
        self.assertEqual(replay.proposal.booking_id, booked.proposal.booking_id)

    async def test_last_night_currency_change_or_missing_stock_is_stale(self):
        inventory = self.db.hotel_inventory
        night = inventory.c.stay_date == self.option.check_out - timedelta(days=1)
        hotel = inventory.c.hotel_id == "demo-airport"
        currency_scope = await self.approve()
        async with self.engine.begin() as connection:
            await connection.execute(update(inventory).where(hotel, night).values(currency="EUR"))
        self.assertEqual((await self.booking.resolve_action(currency_scope)).proposal.status, "stale")
        async with self.engine.begin() as connection:
            await connection.execute(update(inventory).where(hotel, night).values(currency="CHF"))
        business = BusinessRepository(self.engine, self.settings.schema)
        self.selection = BookingSelection(await business.append_message(
            self.owner.user_id, self.owner.thread_id, "demo"),
            0,
        )
        missing_scope = await self.approve()
        async with self.engine.begin() as connection:
            await connection.execute(inventory.delete().where(hotel, night))
        self.assertEqual((await self.booking.resolve_action(missing_scope)).proposal.status, "stale")
        self.assertEqual(await self.snapshot(), ([3], 0, 0))
