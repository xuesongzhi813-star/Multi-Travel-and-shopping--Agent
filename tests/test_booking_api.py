import asyncio
from contextlib import AsyncExitStack
from datetime import timedelta
from unittest.mock import patch
from uuid import UUID, uuid4

from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select, update
from sqlalchemy.schema import DropSchema

from customer_support_chat.app.workflow.checkpoints import GraphUnavailable, prepare_checkpoints
from customer_support_chat.app.workflow.thread_lock import try_thread_lock
from test_booking_repository import BookingRepositoryFixture
from test_business_seed import PASSWORDS
from web_app.app.runtime import create_app


class BookingApiTests(BookingRepositoryFixture):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.checkpoints = f"test_booking_api_{uuid4().hex}"
        self.addAsyncCleanup(self.drop_checkpoints)
        await prepare_checkpoints(self.settings, self.checkpoints)
        self.stack = AsyncExitStack()
        self.addAsyncCleanup(self.stack.aclose)
        self.a, self.app = await self.client(0)
        self.b, _ = await self.client(1)
        self.path = f"/api/threads/{self.owner.thread_id}"
        self.addCleanup(patch.stopall)
        patch("web_app.app.runtime.run_local_tool", side_effect=self.offline_role).start()
        answer = await self.message(self.a, "找 DEMO-A1 的酒店，住 1 晚")
        self.assertEqual(answer.json()["status"], "hotel_results")
        self.message_id = answer.json()["message_id"]

    async def drop_checkpoints(self):
        async with self.engine.begin() as connection:
            await connection.execute(DropSchema(self.checkpoints, cascade=True, if_exists=True))

    async def offline_role(self, operation, *args, **kwargs):
        if operation == "flight":
            return {
                "status": "verified",
                "handoff": {"reference": "DEMO-A1", "arrival_airport": "ZRH",
                            "arrival_date_local": "2026-12-15", "arrival_timezone": "Europe/Zurich",
                            "source_tool_call_id": "flight-proof"},
                "airport": {"status": "ok", "data": {"code": "ZRH", "city": "Zürich"}},
            }
        return {"status": "hotel_results", "options": [self.option.to_json()]}

    async def client(self, account):
        app = create_app(self.settings, checkpoint_schema=self.checkpoints)
        await self.stack.enter_async_context(app.router.lifespan_context(app))
        client = await self.stack.enter_async_context(
            AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1"),
        )
        response = await client.post("/api/auth/login", json={
            "username": ("demo-a", "demo-b")[account], "password": PASSWORDS[account],
        })
        self.assertEqual(response.status_code, 204)
        return client, app

    async def message(self, client, content):
        return await client.post(self.path + "/messages", json={"content": content})

    async def propose(self, client=None, **extra):
        return await (client or self.a).post(self.path + "/hotel-proposals", json={
            "message_id": self.message_id, "option_index": 0, **extra,
        })

    async def start(self):
        response = await self.propose()
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["action_id"]

    async def choose(self, action, decision="approve", client=None, path=None, **extra):
        return await (client or self.a).post((path or self.path) + f"/approvals/{action}/decision", json={
            "decision": decision, **extra,
        })

    async def order_count(self):
        async with self.engine.connect() as connection:
            return await connection.scalar(select(func.count()).select_from(self.db.hotel_bookings))

    async def test_proposal_and_decision_use_trusted_fields_only(self):
        self.assertEqual((await self.propose(self.b)).status_code, 404)
        self.assertEqual((await self.propose(total_minor=1)).status_code, 422)
        self.assertEqual((await self.propose(option_index=True)).status_code, 422)
        self.assertEqual((await self.propose(option_index=5)).status_code, 422)
        self.assertEqual((await self.propose(message_id=str(self.selection.message_id))).status_code, 409)
        action = await self.start()
        self.assertEqual((await self.propose()).status_code, 200)
        self.assertEqual((await self.b.get(self.path)).status_code, 404)
        self.assertEqual((await self.choose(action, client=self.b)).status_code, 404)
        other = (await self.a.post("/api/threads")).json()["thread_id"]
        self.assertEqual((await self.choose(action, path=f"/api/threads/{other}")).status_code, 404)
        self.assertEqual((await self.choose(action, currency="EUR")).status_code, 422)
        self.assertEqual(await self.order_count(), 0)
        rejected = await self.choose(action, "reject")
        self.assertEqual(rejected.json()["status"], "rejected")
        self.assertEqual((await self.choose(action, "reject")).json(), rejected.json())
        self.assertEqual((await self.choose(action)).status_code, 409)
        self.assertEqual(await self.order_count(), 0)

    async def test_pending_approval_blocks_message_before_append(self):
        action = await self.start()
        count = len((await self.a.get(self.path)).json()["messages"])
        self.assertEqual((await self.message(self.a, "继续")).status_code, 409)
        self.assertEqual(len((await self.a.get(self.path)).json()["messages"]), count)
        other, _ = await self.client(0)
        async with try_thread_lock(self.settings, self.checkpoints, self.owner.thread_id):
            self.assertEqual((await self.choose(action, client=other)).status_code, 409)
        results = await asyncio.gather(self.choose(action), self.choose(action, client=other))
        self.assertEqual(sorted(response.status_code for response in results), [200, 409])
        self.assertEqual((await self.choose(action)).json()["status"], "booked")
        self.assertEqual(await self.order_count(), 1)

    async def test_decision_and_receipt_recovery_after_lifespan_restart(self):
        for after_order in (False, True):
            with self.subTest(after_order=after_order):
                action = await self.start()
                original = self.app.state.graph.resume_booking

                async def fail(*args, **kwargs):
                    if after_order:
                        await original(*args, **kwargs)
                    raise GraphUnavailable("injected")

                with patch.object(self.app.state.graph, "resume_booking", side_effect=fail):
                    self.assertEqual((await self.choose(action)).status_code, 503)
                count = await self.order_count()
                self.a, self.app = await self.client(0)
                pending = (await self.a.get(self.path)).json()["booking"]
                self.assertTrue(pending["resume_required"])
                self.assertEqual(pending["decision"], "approve")
                self.assertEqual(await self.order_count(), count)
                receipt = (await self.choose(action)).json()
                self.assertEqual(receipt["status"], "booked")
                self.assertFalse(receipt["resume_required"])
                if after_order:
                    self.assertEqual(receipt["booking_id"], pending["booking_id"])
                resumed = await self.message(self.a, "找 DEMO-A1 的酒店，住 1 晚")
                self.message_id = resumed.json()["message_id"]
        self.assertEqual(await self.order_count(), 2)

    async def test_expired_pending_does_not_block_the_next_message(self):
        action = await self.start()
        table = self.db.hotel_booking_proposals
        async with self.engine.begin() as connection:
            await connection.execute(update(table).where(table.c.action_id == UUID(action)).values(
                created_at=table.c.created_at - timedelta(minutes=20), expires_at=table.c.expires_at - timedelta(minutes=20),
            ))
        self.assertEqual((await self.a.get(self.path)).json()["booking"]["status"], "expired")
        self.assertEqual((await self.message(self.a, "继续")).status_code, 201)
        self.assertEqual((await self.choose(action)).json()["status"], "expired")
        self.assertEqual(await self.order_count(), 0)
