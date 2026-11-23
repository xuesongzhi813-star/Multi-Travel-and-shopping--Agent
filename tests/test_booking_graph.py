from datetime import date
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.schema import DropSchema

from customer_support_chat.app.business.booking import BookingScope, BookingSelection, ProposalConflict, ProposalNotFound
from customer_support_chat.app.business.repository import BusinessRepository
from customer_support_chat.app.tooling.audit import PostgresAuditSink
from customer_support_chat.app.tooling.booking import ApprovedBookingExecutor
from customer_support_chat.app.workflow.checkpoints import open_graph_runtime, prepare_checkpoints
from customer_support_chat.app.workflow.hotel_handoff import HotelRoleResult, VerifiedArrival
from customer_support_chat.app.workflow.thread_lock import try_thread_lock
from test_booking_repository import BookingRepositoryFixture


class BookingGraphTests(BookingRepositoryFixture):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.checkpoint_schema = f"test_booking_graph_{uuid4().hex}"
        self.addAsyncCleanup(self.drop_checkpoints)
        await prepare_checkpoints(self.settings, self.checkpoint_schema)
        self.business = BusinessRepository(self.engine, self.settings.schema)
        self.executor = ApprovedBookingExecutor(self.booking, PostgresAuditSink(self.engine, self.settings.schema))
        message = await self.business.append_message(
            self.owner.user_id, self.owner.thread_id, "找 DEMO-A1 的酒店，住 1 晚",
        )
        self.selection = BookingSelection(message, 0)
        async with self.runtime() as graph:
            result = await graph.invoke(self.owner.user_id, self.owner.thread_id, message)
        self.assertEqual(result["status"], "hotel_results")

    async def drop_checkpoints(self):
        async with self.engine.begin() as connection:
            await connection.execute(DropSchema(self.checkpoint_schema, cascade=True, if_exists=True))

    async def flight(self, user, thread, reference):
        return VerifiedArrival(reference, "Zürich", "ZRH", date(2026, 12, 15), "Europe/Zurich", "flight-proof")

    async def hotels(self, user, thread, query):
        return HotelRoleResult("hotel_results", (self.option,))

    def runtime(self):
        return open_graph_runtime(
            self.settings, self.checkpoint_schema, self.business.read_owned_message, flight_lookup=self.flight,
            hotel_search=self.hotels, booking_repository=self.booking, booking_executor=self.executor,
        )

    def lock(self):
        return try_thread_lock(self.settings, self.checkpoint_schema, self.owner.thread_id)

    async def begin(self):
        async with self.runtime() as graph, self.lock() as connection:
            return await graph.start_proposal(self.owner, self.selection, connection=connection)

    async def test_proposal_and_pause_commit_or_rollback_together(self):
        async with self.runtime() as graph:
            with self.assertRaisesRegex(RuntimeError, "abort"):
                async with self.lock() as connection:
                    proposal = await graph.start_proposal(self.owner, self.selection, connection=connection)
                    saved = await graph.read_booking(self.owner.thread_id, connection=connection)
                    self.assertEqual(saved.action_id, proposal.action_id)
                    self.assertTrue(saved.awaiting_resolution)
                    raise RuntimeError("abort")
            self.assertIsNone(await self.booking.get_proposal(BookingScope(self.owner, proposal.action_id)))
            self.assertFalse((await graph.read_booking(self.owner.thread_id)).awaiting_resolution)
        created = await self.begin()
        async with self.runtime() as restarted:
            self.assertEqual((await restarted.read_booking(self.owner.thread_id)).action_id, created.action_id)
            self.assertEqual((await restarted.read(self.owner.thread_id))["turn_count"], 1)

    async def test_restart_resumes_decision_without_new_message_turn(self):
        proposal = await self.begin()
        scope = BookingScope(self.owner, proposal.action_id)
        await self.booking.record_decision(scope, "approve")
        async with self.runtime() as restarted, self.lock() as connection:
            result = await restarted.resume_booking(scope, connection=connection, request_id=uuid4())
            self.assertEqual(result.status, "booked")
        async with self.runtime() as restarted:
            self.assertFalse((await restarted.read_booking(self.owner.thread_id)).awaiting_resolution)
            self.assertEqual((await restarted.read(self.owner.thread_id))["turn_count"], 1)

    async def test_receipt_commit_survives_checkpoint_rollback(self):
        scope = BookingScope(self.owner, (await self.begin()).action_id)
        await self.booking.record_decision(scope, "approve")
        with self.assertRaisesRegex(RuntimeError, "abort"):
            async with self.runtime() as graph, self.lock() as connection:
                first = await graph.resume_booking(scope, connection=connection, request_id=uuid4())
                self.assertEqual(first.status, "booked")
                raise RuntimeError("abort")
        async with self.runtime() as graph, self.lock() as connection:
            self.assertTrue((await graph.read_booking(self.owner.thread_id, connection=connection)).awaiting_resolution)
            second = await graph.resume_booking(scope, connection=connection, request_id=uuid4())
            self.assertEqual(second.booking_id, first.booking_id)
        async with self.engine.connect() as connection:
            self.assertEqual(await connection.scalar(select(func.count()).select_from(self.db.hotel_bookings)), 1)
            inventory = self.db.hotel_inventory
            self.assertEqual(await connection.scalar(select(inventory.c.quantity).where(
                inventory.c.hotel_id == "demo-airport",
            )), 2)

    async def test_unapproved_or_mismatched_action_cannot_write_and_legacy_graph_stays_readable(self):
        scope = BookingScope(self.owner, (await self.begin()).action_id)
        async with self.runtime() as graph, self.lock() as connection:
            with self.assertRaises(ProposalNotFound):
                await graph.resume_booking(BookingScope(self.owner, uuid4()), connection=connection, request_id=uuid4())
            with self.assertRaises(ProposalConflict):
                await graph.resume_booking(scope, connection=connection, request_id=uuid4())
            self.assertTrue((await graph.read_booking(self.owner.thread_id, connection=connection)).awaiting_resolution)
        async with open_graph_runtime(self.settings, self.checkpoint_schema, self.business.read_owned_message) as legacy:
            self.assertEqual((await legacy.read(self.owner.thread_id))["status"], "hotel_results")
