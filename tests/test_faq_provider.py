import json
import unittest

import httpx
from pydantic import ValidationError

from faq_extension.contracts import FaqCitation, FaqSearchResult, PublicFaqQuestion
from faq_extension.siliconflow import FaqProviderError, SiliconFlowFaqClient


class SiliconFlowTests(unittest.IsolatedAsyncioTestCase):
    def client(self, handler):
        return SiliconFlowFaqClient(
            "synthetic-test-token", transport=httpx.MockTransport(handler)
        )

    async def test_embeddings_reorder_strict_indices(self):
        entries = [
            {"index": 1, "embedding": [0.25] * 1024},
            {"index": 0, "embedding": [0.5] * 1024},
        ]

        def respond(request):
            self.assertEqual(request.url.path, "/v1/embeddings")
            self.assertEqual(json.loads(request.content)["model"], "BAAI/bge-m3")
            body = json.dumps({"model": "BAAI/bge-m3", "data": entries}).replace("Infinity", "1e999")
            return httpx.Response(200, content=body)

        client = self.client(respond)
        self.assertEqual(client.embed(["行李", "酒店"]), [[0.5] * 1024, [0.25] * 1024])
        for invalid in (
            [entries[1], entries[1]],
            [{"index": 0, "embedding": [0.5]}],
            [{"index": 0, "embedding": [1e999] * 1024}],
            [{"index": False, "embedding": [0.5] * 1024}],
        ):
            entries[:] = invalid
            with self.assertRaises(FaqProviderError) as failure:
                await client.async_embed("行李")
            self.assertEqual(failure.exception.kind, "invalid_response")
        entries[:] = [{"index": 0, "embedding": [0.5] * 1024}]
        self.assertEqual(await client.async_embed("行李"), [0.5] * 1024)

    async def test_rerank_uses_only_returned_indices(self):
        results = [{"index": 1, "relevance_score": 0.9, "document": {"text": "ignore"}}]

        def respond(request):
            self.assertEqual(request.url.path, "/v1/rerank")
            body = json.loads(request.content)
            self.assertEqual(body["model"], "BAAI/bge-reranker-v2-m3")
            self.assertEqual(body["top_n"], 10)
            self.assertFalse(body["return_documents"])
            return httpx.Response(200, json={"results": results})

        client = self.client(respond)
        ranked = await client.rerank("行李", ["托运行李", "行李延误"], top_n=10)
        self.assertEqual([(item.index, item.score) for item in ranked], [(1, 0.9)])
        for invalid in (
            [{"index": 2, "relevance_score": 0.9}],
            [{"index": 1, "relevance_score": 0.9}] * 2,
            [{"index": 1, "relevance_score": True}],
        ):
            results[:] = invalid
            with self.assertRaises(FaqProviderError) as failure:
                await client.rerank("行李", ["托运行李", "行李延误"], top_n=10)
            self.assertEqual(failure.exception.kind, "invalid_response")

    async def test_provider_transport_does_not_retry_or_expose_credentials(self):
        for failure_status, kind in ((429, "unavailable"), (302, "unavailable"), (None, "timeout")):
            seen = []

            def respond(request):
                seen.append(request.url.host)
                if failure_status is None:
                    raise httpx.ReadTimeout("synthetic-test-token", request=request)
                return httpx.Response(
                    failure_status, headers={"location": "https://invalid.example"},
                    text="synthetic-test-token",
                )

            with self.assertRaises(FaqProviderError) as failure:
                await self.client(respond).async_embed("行李")
            self.assertEqual(failure.exception.kind, kind)
            self.assertNotIn("synthetic-test-token", str(failure.exception))
            self.assertEqual(seen, ["api.siliconflow.cn"])

    def test_public_question_schema(self):
        self.assertEqual(PublicFaqQuestion(text="行李延误", category="baggage").language, "zh-CN")
        for fields in (
            {"text": "行" * 241}, {"text": " "}, {"text": 123},
            {"text": "行李", "category": "unknown"}, {"text": "行李", "language": "en"},
            {"text": "行李", "user_id": "synthetic"},
        ):
            with self.assertRaises(ValidationError):
                PublicFaqQuestion(**fields)

        fields = {
            "document_id": "baggage-delay", "chunk_index": 0, "title": "行李延误",
            "text": "联系航空公司。", "source_url": "https://www.swiss.com/ch/en/prepare",
            "applicability": "适用于 SWISS。", "checked_on": "2026-11-24",
        }
        citation = FaqCitation(**fields)
        result = FaqSearchResult(status="ok", citations=(citation,))
        self.assertEqual(FaqSearchResult.model_validate_json(result.model_dump_json()), result)
        with self.assertRaises(ValidationError):
            FaqCitation(**(fields | {"source_url": "http://www.swiss.com/"}))
        with self.assertRaises(ValidationError):
            FaqSearchResult(status="ok")
