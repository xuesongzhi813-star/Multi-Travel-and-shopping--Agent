"""Explicit, bounded SiliconFlow verification of the published FAQ pipeline."""

import argparse
import asyncio
from collections.abc import Awaitable, Callable
from contextlib import contextmanager
import json
from pathlib import Path
import sys
from uuid import uuid4

import httpx
from pymysql import MySQLError


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from customer_support_chat.app.business.database import local_value
from customer_support_chat.app.workflow.faq_turn import classify_faq_turn
from faq_extension.catalog import load_faq_version
from faq_extension.contracts import PublicFaqQuestion
from faq_extension.mysql_catalog import MySqlFaqCatalog, MySqlFaqSettings
from faq_extension.publication import FaqPublisher, PublicationError
from faq_extension.retrieval import FaqSearchService
from faq_extension.siliconflow import PROVIDER_URL, REQUEST_TIMEOUT, SiliconFlowFaqClient


MAX_REQUESTS = 10
ALLOWED_PATHS = frozenset({"/v1/embeddings", "/v1/rerank"})
SOURCE = ROOT / "faq_extension/data/travel_faq_zh.json"
Send = Callable[[httpx.Request], Awaitable[httpx.Response]]


async def _send_provider(request: httpx.Request) -> httpx.Response:
    async with httpx.AsyncHTTPTransport(retries=0, trust_env=False) as transport:
        response = await transport.handle_async_request(request)
        try:
            return httpx.Response(response.status_code, content=await response.aread())
        finally:
            await response.aclose()


class RequestBudgetTransport(httpx.MockTransport):
    def __init__(self, limit: int, *, send: Send = _send_provider):
        if type(limit) is not int or not 1 <= limit <= MAX_REQUESTS:
            raise ValueError("Invalid SiliconFlow request budget")
        self.limit = limit
        self.attempts = 0
        self.codes: list[int] = []
        self.send = send
        super().__init__(self._send_with_budget)

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        return asyncio.run(self.handle_async_request(request))

    async def _send_with_budget(self, request: httpx.Request) -> httpx.Response:
        if (request.url.scheme != "https" or request.url.host != httpx.URL(PROVIDER_URL).host
                or request.url.port not in (None, 443) or request.method != "POST"
                or request.url.path not in ALLOWED_PATHS or self.attempts >= self.limit):
            raise httpx.RequestError("SiliconFlow verification request denied", request=request)
        self.attempts += 1
        try:
            async with asyncio.timeout(REQUEST_TIMEOUT):
                response = await self.send(request)
        except TimeoutError:
            raise httpx.ReadTimeout("SiliconFlow verification deadline", request=request) from None
        self.codes.append(response.status_code)
        return response


@contextmanager
def temporary_catalog(client: httpx.Client, settings: MySqlFaqSettings):
    prefix = "e2e_p9_faq_" + uuid4().hex[:18]
    catalog = MySqlFaqCatalog(settings, table_prefix=prefix)
    try:
        catalog.initialize()
        yield catalog, prefix
    finally:
        try:
            collections = client.get("/collections").raise_for_status().json()["result"]["collections"]
            for item in collections:
                name = item["name"]
                if name.startswith(prefix + "_"):
                    client.delete(f"/collections/{name}").raise_for_status()
        finally:
            with catalog._transaction() as cursor:
                for suffix in ("history", "active", "index_profiles", "builds", "documents", "versions"):
                    cursor.execute(f"DROP TABLE IF EXISTS {catalog._table(suffix)}")


async def check_questions(
    base_url: str, catalog: MySqlFaqCatalog, provider: SiliconFlowFaqClient, case: str,
) -> dict:
    async with httpx.AsyncClient(base_url=base_url, timeout=8, trust_env=False) as qdrant:
        search = FaqSearchService(catalog, qdrant, provider)
        cases = (
            ("baggage_synonym", PublicFaqQuestion(text="托运行李没有收到 行李延误", category="baggage"), "bag-"),
            ("policy_scope", classify_faq_turn(
                "SWISS 航班取消后没有合适的替代航班，在哪里查看退款选项？"
            ).question, "refund-after-cancel"),
            ("no_answer", PublicFaqQuestion(text="月球旅馆允许带滑雪板吗"), None),
        )
        outcomes = {}
        for name, question, expected in cases:
            if case not in ("all", name):
                continue
            result = await search.search(question, deadline=asyncio.get_running_loop().time() + 24)
            expected_status = "no_evidence" if expected is None else "ok"
            first = next(iter(result.citations), None)
            if name == "policy_scope":
                mapped = bool(first and first.document_id == expected and "SWISS 航班取消" in first.applicability
                              and "可选的新航班不合适" in first.applicability)
            else:
                mapped = expected is None or any(citation.document_id.startswith(expected) for citation in result.citations)
            outcomes[name] = {
                "status": result.status, "citations": len(result.citations),
                "first_document_id": first.document_id if first else None,
                "passed": result.status == expected_status and mapped and result.degradation is None,
            }
        return outcomes


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--max-requests", type=int, choices=range(1, MAX_REQUESTS + 1), required=True)
    parser.add_argument("--case", choices=("all", "baggage_synonym", "policy_scope", "no_answer"), default="all")
    args = parser.parse_args(argv)
    key = local_value("SILICONFLOW_API_KEY").strip()
    if not key:
        print(json.dumps({"status": "credential_missing", "provider_requests": 0}))
        return 2
    transport = RequestBudgetTransport(args.max_requests)
    provider = SiliconFlowFaqClient(key, transport=transport)
    outcomes = {}
    try:
        settings = MySqlFaqSettings.read()
        port = int(local_value("QDRANT_PORT", "6333"))
        if not 1 <= port <= 65535:
            raise ValueError("Invalid local Qdrant port")
        base_url = f"http://127.0.0.1:{port}"
        with httpx.Client(base_url=base_url, timeout=8, trust_env=False) as client:
            with temporary_catalog(client, settings) as (catalog, prefix):
                version = load_faq_version(SOURCE)
                catalog.import_version(version)
                publisher = FaqPublisher(catalog, client, collection_prefix=prefix, embedder=provider.embed)
                build = publisher.build(version.version_id, hybrid=True)
                publisher.activate(build.build_id, expected_revision=0)
                outcomes = asyncio.run(check_questions(base_url, catalog, provider, args.case))
        passed = bool(outcomes) and all(item["passed"] for item in outcomes.values())
        status = "passed" if passed else "acceptance_failed"
    except (MySQLError, PublicationError, httpx.HTTPError, OSError, ValueError, KeyError):
        status = "verification_failed"
        passed = False
    print(json.dumps({
        "status": status, "provider_requests": transport.attempts,
        "http_statuses": transport.codes, "questions": outcomes,
    }))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
