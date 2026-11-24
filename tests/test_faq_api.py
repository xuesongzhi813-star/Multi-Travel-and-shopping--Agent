from contextlib import AsyncExitStack
import socket
import unittest
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

import httpx
from sqlalchemy.schema import DropSchema

from customer_support_chat.app.business.database import local_value
from customer_support_chat.app.business.demo_seed import seed_demo
from customer_support_chat.app.workflow.checkpoints import prepare_checkpoints
from faq_extension.contracts import FaqSearchResult
from faq_extension.catalog import load_faq_version
from faq_extension.mysql_catalog import MySqlFaqSettings
from faq_extension.publication import FaqPublisher
from faq_extension.siliconflow import FaqProviderError, RankedIndex
from scripts.verify_faq_siliconflow import SOURCE, RequestBudgetTransport, temporary_catalog
import test_business_migrations
from test_business_seed import PASSWORDS
from test_faq_graph import evidence
from web_app.app.runtime import create_app


class FaqApiTests(test_business_migrations.BusinessDatabaseTest):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await seed_demo(self.engine, self.settings.schema, PASSWORDS)
        self.checkpoints = f"test_faq_api_{uuid4().hex}"
        self.addAsyncCleanup(self.drop_checkpoints)
        await prepare_checkpoints(self.settings, self.checkpoints)
        self.stack = AsyncExitStack()
        self.addAsyncCleanup(self.stack.aclose)
        self.search = AsyncMock(return_value=evidence())
        self.app = create_app(self.settings, checkpoint_schema=self.checkpoints, faq_search=self.search)
        await self.stack.enter_async_context(self.app.router.lifespan_context(self.app))
        self.client = await self.stack.enter_async_context(httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://127.0.0.1",
        ))
        login = await self.client.post("/api/auth/login", json={"username": "demo-a", "password": PASSWORDS[0]})
        self.assertEqual(login.status_code, 204)
        response = await self.client.post("/api/threads")
        self.assertEqual(response.status_code, 201)
        self.path = "/api/threads/" + response.json()["thread_id"]

    async def drop_checkpoints(self):
        async with self.engine.begin() as connection:
            await connection.execute(DropSchema(self.checkpoints, cascade=True, if_exists=True))

    async def send(self, text):
        response = await self.client.post(self.path + "/messages", json={"content": text})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    async def test_real_message_and_session_projection(self):
        result = await self.send("行李延误怎么办？")
        self.assertEqual(result["status"], "faq")
        self.assertEqual(result["faq"]["citations"][0]["source_url"], evidence().citations[0].source_url)
        thread = (await self.client.get(self.path)).json()
        self.assertEqual(thread["graph"]["faq"], result["faq"])
        home = await self.client.get("/")
        self.assertIn('id="faq-evidence"', home.text)
        self.assertIn("renderFaq(state.graph)", home.text)

    async def test_mixed_input_never_sends_private_tokens(self):
        flight = await self.send("查 DEMO-A1 航班，顺便问行李延误怎么办？")
        self.assertIn("已核验航班", flight["reply"])
        self.assertEqual(flight["faq"]["status"], "ok")
        hotel = await self.send("找 DEMO-A1 的酒店，住 1 晚，顺便问酒店设施怎么核实？")
        self.assertEqual(hotel["status"], "hotel_results")
        self.assertEqual(hotel["faq"]["status"], "ok")
        self.assertEqual([call.args[0].category for call in self.search.await_args_list], ["baggage", "lodging"])
        self.assertNotIn("DEMO-A1", repr(self.search.await_args_list))
        blocked = await self.send("订单号 ABC12345 的行李延误怎么办？")
        self.assertEqual(blocked["faq"]["status"], "rephrase")
        self.assertEqual(self.search.await_count, 2)

    async def test_inactive_index_redis_and_reranker_fallbacks(self):
        for result in (
            FaqSearchResult(status="not_published"),
            FaqSearchResult(status="no_evidence"),
            evidence().model_copy(update={"degradation": "reranker_timeout"}),
        ):
            self.search.return_value = result
            response = await self.send("行李延误怎么办？")
            self.assertEqual(response["faq"], result.model_dump(mode="json"))

    async def test_old_graph_result_and_pending_approval(self):
        self.assertNotIn("faq", await self.send("普通消息"))
        hotel = await self.send("找 DEMO-A1 的酒店，住 1 晚")
        proposal = await self.client.post(
            self.path + "/hotel-proposals", json={"message_id": hotel["message_id"], "option_index": 0},
        )
        self.assertEqual(proposal.status_code, 201)
        blocked = await self.client.post(self.path + "/messages", json={"content": "行李延误怎么办？"})
        self.assertEqual(blocked.status_code, 409)
        self.search.assert_not_awaited()


    async def test_production_search_returns_citations_with_redis_and_reranker_outages(self):
        port = local_value("QDRANT_PORT", "6333")
        with (
            httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=8, trust_env=False) as qdrant,
            temporary_catalog(qdrant, MySqlFaqSettings.read()) as (catalog, prefix),
            socket.socket() as unused,
        ):
            unused.bind(("127.0.0.1", 0))
            vector = [1.0] + [0.0] * 1023
            version = load_faq_version(SOURCE)
            catalog.import_version(version)
            publisher = FaqPublisher(catalog, qdrant, collection_prefix=prefix,
                                     embedder=lambda texts: [vector for _ in texts])
            build = publisher.build(version.version_id, hybrid=True)
            publisher.activate(build.build_id, expected_revision=0)
            provider = Mock()
            provider.async_embed = AsyncMock(return_value=vector)
            provider.rerank = AsyncMock(return_value=[RankedIndex(0, 0.9)])
            settings = {
                "TRAVEL_FAQ_MODE": "siliconflow", "SILICONFLOW_API_KEY": "synthetic-fixture-token",
                "REDIS_PORT": str(unused.getsockname()[1]),
            }

            def fixture_value(name, default=""):
                return settings[name] if name in settings else local_value(name, default)

            with (
                patch("web_app.app.runtime.MySqlFaqCatalog", return_value=catalog),
                patch("web_app.app.runtime.SiliconFlowFaqClient", return_value=provider),
                patch("web_app.app.runtime.local_value", side_effect=fixture_value),
            ):
                production = create_app(self.settings, checkpoint_schema=self.checkpoints)
                async with (
                    production.router.lifespan_context(production),
                    httpx.AsyncClient(transport=httpx.ASGITransport(app=production),
                                      base_url="http://127.0.0.1", cookies=self.client.cookies) as http,
                ):
                    for degraded in (False, True):
                        if degraded:
                            provider.rerank.side_effect = FaqProviderError("timeout")
                        response = await http.post(self.path + "/messages", json={"content": "行李延误怎么办？"})
                        self.assertEqual(response.status_code, 201)
                        faq = response.json()["faq"]
                        self.assertEqual(faq["status"], "ok")
                        self.assertTrue(faq["citations"][0]["document_id"].startswith("bag-"))
                        self.assertEqual(faq["degradation"], "reranker_timeout" if degraded else None)

class FaqRequestBudgetTests(unittest.IsolatedAsyncioTestCase):
    async def test_eleventh_request_is_rejected_without_retry_or_redirect(self):
        sender = AsyncMock(side_effect=[
            httpx.Response(302, headers={"Location": "https://invalid.example"}),
            httpx.Response(429),
            *[httpx.Response(200) for _ in range(8)],
        ])
        transport = RequestBudgetTransport(10, send=sender)
        async with httpx.AsyncClient(transport=transport, follow_redirects=False) as client:
            for expected in [302, 429, *[200] * 8]:
                response = await client.post("https://api.siliconflow.cn/v1/embeddings", json={})
                self.assertEqual(response.status_code, expected)
            with self.assertRaises(httpx.RequestError):
                await client.post("https://api.siliconflow.cn/v1/embeddings", json={})
        self.assertEqual(sender.await_count, 10)
        self.assertEqual(transport.attempts, 10)
