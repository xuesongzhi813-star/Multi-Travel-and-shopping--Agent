import asyncio
from io import StringIO
from datetime import date
import json
from threading import Event
import unittest
from uuid import uuid4

from sqlalchemy import select
from unittest.mock import AsyncMock, patch

from customer_support_chat.app.business.demo_seed import seed_demo
from customer_support_chat.app.business.repository import BusinessRepository
from customer_support_chat.app.workflow.hotel_handoff import HotelQuery
import test_business_migrations
import test_business_seed
from faq_extension.contracts import FaqSearchResult, PublicFaqQuestion
from web_app.app.local_tools import LocalToolError, LocalToolTimeout, run_faq_role_process, run_local_tool


QUESTION = PublicFaqQuestion(text="行李延误", category="baggage")
STARTUP = json.dumps({"operation": "flight", "faq_question": QUESTION.model_dump(mode="json")})


class FakeProcess:
    def __init__(self, frames=(), *, block=False):
        self.stdin = StringIO()
        self.done = Event()
        self.stdout = self if block else StringIO("".join(json.dumps(frame) + "\n" for frame in frames))
        self.killed = False
        self.waited = False
        self.returncode = None

    def readline(self, limit):
        self.done.wait(5)
        return ""

    def close(self):
        pass

    def poll(self):
        return self.returncode

    def kill(self):
        self.killed = True
        self.done.set()

    def wait(self):
        self.waited = True
        self.returncode = 1 if self.killed else 0
        return self.returncode


def request(text="行李延误"):
    return {"type": "faq_request", "role": "flight", "question": QUESTION.model_dump(mode="json") | {"text": text}}


class FaqWorkerBridgeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.lookup = AsyncMock(return_value=FaqSearchResult(status="no_evidence"))

    async def run_worker(self, process, *, timeout=1.0):
        with patch("web_app.app.local_tools.subprocess.Popen", return_value=process):
            return await run_faq_role_process(
                {}, STARTUP, timeout=timeout, question=QUESTION, lookup=self.lookup,
            )

    async def test_bridge_rejects_modified_question_and_duplicate_frame(self):
        for frames, calls in (((request("私有记录"),), 0), ((request(), request()), 1)):
            self.lookup.reset_mock()
            process = FakeProcess(frames)
            with self.assertRaises(LocalToolError):
                await self.run_worker(process)
            self.assertEqual(self.lookup.await_count, calls)
            self.assertTrue(process.waited)

    async def test_timeout_cancellation_reap_worker_and_release_slot(self):
        for cancelled in (False, True):
            process = FakeProcess(block=True)
            worker = asyncio.create_task(self.run_worker(process, timeout=0.2))
            if cancelled:
                await asyncio.sleep(0.05)
                worker.cancel()
            with self.assertRaises(asyncio.CancelledError if cancelled else LocalToolTimeout):
                await worker
            self.assertTrue(process.killed and process.waited)
        result = await self.run_worker(FakeProcess([request(), {"type": "result", "result": {"status": "verified"}}]))
        self.assertEqual(result["status"], "verified")


class RealFaqWorkerBridgeTests(test_business_migrations.BusinessDatabaseTest):
    async def test_both_real_workflows_call_host_once_and_keep_receipts(self):
        await seed_demo(self.engine, self.settings.schema, test_business_seed.PASSWORDS)
        async with self.engine.connect() as connection:
            user = await connection.scalar(select(self.db.users.c.id).where(self.db.users.c.username == "demo-a"))
        repository = BusinessRepository(self.engine, self.settings.schema)
        thread = await repository.create_thread(user)
        lookup = AsyncMock(return_value=FaqSearchResult(status="no_evidence"))
        flight = await run_local_tool(
            "flight", user, "DEMO-A1", self.settings, uuid4(), flight_mode="offline", thread_id=thread,
            faq_question=QUESTION, faq_lookup=lookup,
        )
        self.assertEqual(flight["status"], "verified")
        self.assertEqual(flight["airport"]["status"], "ok")
        self.assertEqual(flight["faq"]["status"], "no_evidence")
        stay = HotelQuery("Zürich", "Europe/Zurich", date(2026, 12, 15), date(2026, 12, 16))
        hotel = await run_local_tool(
            "hotel", user, stay.to_json(), self.settings, uuid4(), flight_mode="offline", thread_id=thread,
            faq_question=QUESTION, faq_lookup=lookup,
        )
        self.assertEqual(hotel["status"], "hotel_results")
        self.assertEqual(hotel["faq"]["status"], "no_evidence")
        self.assertEqual(lookup.await_count, 2)
        self.assertEqual([item.args for item in lookup.await_args_list], [(QUESTION,), (QUESTION,)])
