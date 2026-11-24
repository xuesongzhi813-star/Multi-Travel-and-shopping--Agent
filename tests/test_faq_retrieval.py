import asyncio
import json
import unittest
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import httpx
from redis.asyncio import Redis

from customer_support_chat.app.business.database import local_value
from faq_extension.cache import FaqEvidenceCache
from faq_extension.contracts import PublicFaqQuestion
from faq_extension.mysql_catalog import PublicationPointer
from faq_extension.publication import FaqPublisher, HYBRID_PROFILE
from faq_extension.retrieval import FaqSearchService
from faq_extension.siliconflow import FaqProviderError, RankedIndex
import test_faq_mysql


QUESTION = PublicFaqQuestion(text="行李延误", category="baggage")
VECTOR = [1.0] + [0.0] * 1023


def row(index=0, **changes):
    return {"id": str(uuid4()), "payload": {
        "document_id": f"bag-{index}", "chunk_index": 0,
        "question": f"行李延误办法 {index}", "chunk_text": "联系航空公司。",
        "version_id": "travel-v1", "category": "baggage", "language": "zh-CN",
        "source_url": "https://www.swiss.com/ch/en/prepare", "applicability": "适用于 SWISS。",
        "checked_on": "2026-11-24",
    } | changes}


class FaqRetrievalTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.build_id = uuid4()
        self.pointer = PublicationPointer(
            1, "travel-v1", self.build_id, "travel_faq_" + self.build_id.hex, HYBRID_PROFILE,
        )
        self.catalog = Mock()
        self.catalog.active.return_value = self.pointer
        self.provider = Mock()
        self.provider.async_embed = AsyncMock(return_value=VECTOR)
        self.provider.rerank = AsyncMock(return_value=[RankedIndex(0, 0.9)])
        self.rows = [row()]
        self.requests = []

        def respond(request):
            self.requests.append(json.loads(request.content))
            return httpx.Response(200, json={"status": "ok", "result": {"points": self.rows}})

        qdrant = httpx.AsyncClient(base_url="http://test.invalid", transport=httpx.MockTransport(respond))
        self.addAsyncCleanup(qdrant.aclose)
        self.service = FaqSearchService(self.catalog, qdrant, self.provider)

    async def search(self):
        return await self.service.search(QUESTION, deadline=asyncio.get_running_loop().time() + 5)

    async def test_single_snapshot_two_prefetch_and_rrf(self):
        result = await self.search()
        self.assertEqual(result.status, "ok")
        self.catalog.active.assert_called_once_with()
        request, = self.requests
        self.assertEqual(request["query"], {"rrf": {"k": 60}})
        self.assertEqual(request["limit"], 40)
        self.assertEqual([item["limit"] for item in request["prefetch"]], [100, 100])
        self.assertEqual([item["using"] for item in request["prefetch"]], ["dense", "search_text"])
        self.assertEqual([item["filter"] for item in request["prefetch"]], [request["filter"]] * 2)
        self.assertEqual(
            {item["key"]: item["match"]["value"] for item in request["filter"]["must"]},
            {"version_id": "travel-v1", "category": "baggage", "language": "zh-CN"},
        )

    async def test_rerank_threshold_and_citations(self):
        self.rows = [row(index) for index in range(12)]
        self.rows.insert(1, self.rows[0])
        self.provider.rerank.return_value = [RankedIndex(0, 0.34)] + [
            RankedIndex(index, 0.7) for index in range(1, 10)
        ]
        result = await self.search()
        self.assertEqual([citation.document_id for citation in result.citations], [f"bag-{n}" for n in range(1, 7)])
        self.assertEqual(self.provider.rerank.await_args.kwargs["top_n"], 10)
        self.assertEqual(len(self.provider.rerank.await_args.args[1]), 12)

    async def test_rrf_fallback_requires_topic_match(self):
        self.rows = [row(), row(1, question="行李损坏"), row(2, category="lodging")]
        for kind in ("timeout", "invalid_response"):
            self.provider.rerank.side_effect = FaqProviderError(kind)
            result = await self.search()
            self.assertEqual(result.degradation, f"reranker_{kind}")
            self.assertEqual([x.document_id for x in result.citations], ["bag-0"])

    async def test_out_of_scope_does_not_invent_policy(self):
        self.rows = [row(source_url="https://invalid.example"), row(1, version_id="old"), row(2, language="en")]
        result = await self.search()
        self.assertEqual((result.status, result.citations), ("no_evidence", ()))
        self.provider.rerank.assert_not_awaited()

    async def open_cache(self):
        port = int(local_value("REDIS_PORT", "6379"))
        redis = Redis(host="127.0.0.1", port=port, socket_timeout=2, socket_connect_timeout=2)
        self.addAsyncCleanup(redis.aclose)
        namespace = "test:faq:" + uuid4().hex

        async def remove_keys():
            async for key in redis.scan_iter(match=namespace + ":*"):
                await redis.delete(key)

        self.addAsyncCleanup(remove_keys)
        return redis, namespace, FaqEvidenceCache(redis, namespace=namespace)

    async def test_cache_miss_after_rebuild_and_aba(self):
        redis, namespace, self.service.cache = await self.open_cache()
        other = uuid4()
        for revision, build in ((1, self.build_id), (2, other), (3, self.build_id)):
            self.catalog.active.return_value = PublicationPointer(
                revision, "travel-v1", build, "travel_faq_" + build.hex, HYBRID_PROFILE,
            )
            self.assertEqual((await self.search()).status, "ok")
            self.assertEqual((await self.search()).status, "ok")
        self.assertEqual(self.provider.async_embed.await_count, 3)
        keys = [key async for key in redis.scan_iter(match=namespace + ":*")]
        self.assertEqual(len(keys), 3)
        for key in keys:
            self.assertNotIn(QUESTION.text.encode(), key)
            self.assertIn(await redis.ttl(key), range(890, 901))

    async def test_cache_timeout_and_malformed_data_are_misses(self):
        redis, namespace, cache = await self.open_cache()
        await redis.set(f"{namespace}:bad", b'{"status":"ok","citations":[]}')
        self.assertIsNone(await cache.get("bad"))
        slow = Mock()

        async def wait_forever(_):
            await asyncio.sleep(60)

        slow.get = wait_forever
        self.service.cache = FaqEvidenceCache(slow, namespace=namespace)
        slow.set = AsyncMock()
        started = asyncio.get_running_loop().time()
        self.assertEqual((await self.search()).status, "ok")
        self.assertLess(asyncio.get_running_loop().time() - started, 2)


    async def test_cache_write_cannot_replace_a_valid_result_at_deadline(self):
        cache = Mock()
        cache.get = AsyncMock(return_value=None)

        async def slow_put(*args):
            await asyncio.sleep(60)

        cache.put = AsyncMock(side_effect=slow_put)
        self.service.cache = cache
        result = await self.service.search(QUESTION, deadline=asyncio.get_running_loop().time() + 0.15)
        self.assertEqual(result.status, "ok")
        cache.put.assert_not_awaited()

class RealQdrantFaqRetrievalTests(test_faq_mysql.MySqlFixture):
    def test_hybrid_query_runs_against_published_qdrant(self):
        base_url = "http://127.0.0.1:" + local_value("QDRANT_PORT", "6333")
        client = httpx.Client(base_url=base_url, timeout=8, trust_env=False)
        self.addCleanup(client.close)
        publisher = FaqPublisher(
            self.catalog, client, collection_prefix=self.prefix, embedder=lambda texts: [VECTOR for _ in texts],
        )
        self.catalog.import_version(self.release)

        def drop_collections():
            names = client.get("/collections").raise_for_status().json()["result"]["collections"]
            for item in names:
                if item["name"].startswith(self.prefix + "_"):
                    client.delete("/collections/" + item["name"]).raise_for_status()

        self.addCleanup(drop_collections)
        build = publisher.build(self.release.version_id, hybrid=True)
        publisher.activate(build.build_id, expected_revision=0)

        async def retrieve():
            async with httpx.AsyncClient(base_url=base_url, timeout=8, trust_env=False) as qdrant:
                provider = Mock()
                provider.async_embed = AsyncMock(return_value=VECTOR)
                provider.rerank = AsyncMock(return_value=[RankedIndex(0, 0.9)])
                service = FaqSearchService(self.catalog, qdrant, provider)
                return await service.search(QUESTION, deadline=asyncio.get_running_loop().time() + 8)

        result = asyncio.run(retrieve())
        self.assertEqual(result.status, "ok")
        self.assertTrue(result.citations[0].document_id.startswith("bag-"))
