"""Search one published FAQ snapshot and return validated policy evidence."""

import asyncio
import re
from unicodedata import normalize

import httpx
from pydantic import ValidationError
from pymysql import MySQLError

from .cache import REDIS_BUDGET, FaqEvidenceCache
from .catalog import digest_json
from .contracts import FaqCitation, FaqSearchResult, PublicFaqQuestion
from .mysql_catalog import CatalogConflict, CatalogNotFound, MySqlFaqCatalog, PublicationPointer
from .publication import FAQ_BM25, FAQ_DENSE, HYBRID_PROFILE
from .siliconflow import FaqProviderError, RERANK_MODEL, SiliconFlowFaqClient


PREFETCH_LIMIT = 100
RRF_CONSTANT = 60
FUSED_LIMIT = 40
RERANK_LIMIT = 10
RERANK_THRESHOLD = 0.35
CITATION_LIMIT = 6
COLLECTION = re.compile(r"[a-z][a-z0-9_]{0,38}_[0-9a-f]{32}")
SEARCH_CONFIG = {
    "profile": "hybrid-v1", "prefetch": PREFETCH_LIMIT, "rrf": RRF_CONSTANT,
    "fused": FUSED_LIMIT, "rerank_model": RERANK_MODEL, "rerank_limit": RERANK_LIMIT,
    "threshold": RERANK_THRESHOLD, "citations": CITATION_LIMIT,
}
PAYLOAD_FIELDS = (
    "document_id", "chunk_index", "question", "chunk_text", "applicability", "source_url",
    "checked_on", "category", "version_id", "language",
)


def _normalized(text: str) -> str:
    return " ".join(normalize("NFKC", text).casefold().split())


def _citation(payload: object, filters: dict) -> FaqCitation | None:
    if not isinstance(payload, dict) or any(payload.get(key) != value for key, value in filters.items()):
        return None
    try:
        return FaqCitation(
            document_id=payload["document_id"], chunk_index=payload["chunk_index"], title=payload["question"],
            text=payload["chunk_text"], applicability=payload["applicability"],
            source_url=payload["source_url"], checked_on=payload["checked_on"],
        )
    except (KeyError, ValidationError):
        return None


def _topics_match(question: str, citation: FaqCitation) -> bool:
    # The public question is built from approved topic phrases. Require its
    # specific topic phrase in the evidence when the reranker is unavailable.
    topic = re.search(r"[\u3400-\u9fff]{2,}|[a-z0-9]{3,}", _normalized(question))
    return bool(topic and topic.group() in _normalized(citation.title + " " + citation.text))


class FaqSearchService:
    def __init__(
        self, catalog: MySqlFaqCatalog, qdrant: httpx.AsyncClient,
        provider: SiliconFlowFaqClient | None, cache: FaqEvidenceCache | None = None,
    ):
        self.catalog = catalog
        self.qdrant = qdrant
        self.provider = provider
        self.cache = cache
        self._catalog_slots = asyncio.Semaphore(2)

    async def _snapshot(self) -> PublicationPointer:
        await self._catalog_slots.acquire()
        read = asyncio.create_task(asyncio.to_thread(self.catalog.active))

        def release(finished: asyncio.Task) -> None:
            self._catalog_slots.release()
            if not finished.cancelled():
                finished.exception()

        read.add_done_callback(release)
        return await asyncio.shield(read)

    async def search(self, question: PublicFaqQuestion, *, deadline: float) -> FaqSearchResult:
        try:
            async with asyncio.timeout_at(deadline):
                pointer = await self._snapshot()
                if pointer.profile.kind == "evidence" or pointer.build_id is None or pointer.version_id is None:
                    return FaqSearchResult(status="not_published")
                if pointer.profile != HYBRID_PROFILE or not COLLECTION.fullmatch(pointer.collection or ""):
                    return FaqSearchResult(status="unavailable")

                filters = {"version_id": pointer.version_id, "language": question.language}
                if question.category is not None:
                    filters["category"] = question.category
                text = _normalized(question.text)
                cache_key = digest_json({
                    "revision": pointer.revision, "build_id": str(pointer.build_id), "question": text,
                    "filters": filters, "config": SEARCH_CONFIG,
                })
                if self.cache is not None:
                    cached = await self.cache.get(cache_key)
                    if cached is not None:
                        return cached
                if self.provider is None:
                    return FaqSearchResult(status="unavailable")
                candidates = await self._recall(pointer.collection, text, filters)
                result = await self._rerank(text, candidates, deadline)
                remaining = deadline - asyncio.get_running_loop().time()
                if self.cache is not None and remaining > REDIS_BUDGET + 0.01:
                    await self.cache.put(cache_key, result)
                return result
        except CatalogNotFound:
            return FaqSearchResult(status="not_published")
        except (TimeoutError, CatalogConflict, MySQLError, httpx.HTTPError, FaqProviderError, ValueError):
            return FaqSearchResult(status="unavailable")

    async def _recall(self, collection: str, text: str, filters: dict[str, str]) -> list[FaqCitation]:
        vector = await self.provider.async_embed(text)
        constraints = {"must": [{"key": key, "match": {"value": value}} for key, value in filters.items()]}
        response = await self.qdrant.post(f"/collections/{collection}/points/query", json={
            "prefetch": [
                {"query": vector, "using": FAQ_DENSE, "filter": constraints, "limit": PREFETCH_LIMIT},
                {"query": {"text": text}, "using": FAQ_BM25, "filter": constraints, "limit": PREFETCH_LIMIT},
            ],
            "query": {"rrf": {"k": RRF_CONSTANT}}, "filter": constraints, "limit": FUSED_LIMIT,
            "with_payload": list(PAYLOAD_FIELDS), "with_vector": False,
        })
        payload = response.raise_for_status().json()
        result = payload.get("result") if isinstance(payload, dict) and payload.get("status") == "ok" else None
        points = result.get("points") if isinstance(result, dict) else None
        if not isinstance(points, list) or len(points) > FUSED_LIMIT:
            raise ValueError("Invalid FAQ search response")
        citations = {}
        for point in points:
            citation = _citation(point.get("payload"), filters) if isinstance(point, dict) else None
            if citation:
                citations.setdefault((citation.document_id, citation.chunk_index), citation)
        return list(citations.values())

    async def _rerank(self, text: str, candidates: list[FaqCitation], deadline: float) -> FaqSearchResult:
        if not candidates:
            return FaqSearchResult(status="no_evidence")
        try:
            async with asyncio.timeout_at(min(deadline - 0.03, asyncio.get_running_loop().time() + 3)):
                ranked = await self.provider.rerank(
                    text, [f"{citation.title}\n{citation.text}" for citation in candidates], top_n=RERANK_LIMIT,
                )
        except TimeoutError:
            degradation = "reranker_timeout"
        except FaqProviderError as error:
            degradation = {
                "timeout": "reranker_timeout",
                "unavailable": "reranker_unavailable",
                "invalid_response": "reranker_invalid_response",
            }[error.kind]
        else:
            evidence = tuple(
                candidates[item.index] for item in ranked if item.score >= RERANK_THRESHOLD
            )[:CITATION_LIMIT]
            return FaqSearchResult(status="ok" if evidence else "no_evidence", citations=evidence)
        evidence = tuple(candidate for candidate in candidates if _topics_match(text, candidate))[:CITATION_LIMIT]
        return FaqSearchResult(
            status="ok" if evidence else "no_evidence", citations=evidence, degradation=degradation,
        )
