"""Bounded SiliconFlow requests for public FAQ retrieval."""

from collections.abc import Sequence
from dataclasses import dataclass
import math
from typing import Literal

import httpx


EMBEDDING_MODEL = "BAAI/bge-m3"
RERANK_MODEL = "BAAI/bge-reranker-v2-m3"
EMBEDDING_DIMENSIONS = 1024
EMBEDDING_BATCH_SIZE = 24
PROVIDER_URL = "https://api.siliconflow.cn"
REQUEST_TIMEOUT = 10.0
ProviderErrorKind = Literal["timeout", "unavailable", "invalid_response"]


class FaqProviderError(RuntimeError):
    def __init__(self, kind: ProviderErrorKind):
        self.kind = kind
        super().__init__(kind)


@dataclass(frozen=True)
class RankedIndex:
    index: int
    score: float


def _number(value: object) -> float:
    if type(value) not in (int, float):
        raise FaqProviderError("invalid_response")
    try:
        number = float(value)
    except OverflowError:
        raise FaqProviderError("invalid_response") from None
    if not math.isfinite(number):
        raise FaqProviderError("invalid_response")
    return number


def _rows(response: httpx.Response, field: str) -> tuple[dict, list]:
    if not response.is_success:
        raise FaqProviderError("unavailable")
    try:
        payload = response.json()
    except (ValueError, RecursionError):
        raise FaqProviderError("invalid_response") from None
    if not isinstance(payload, dict) or not isinstance(payload.get(field), list):
        raise FaqProviderError("invalid_response")
    return payload, payload[field]


def _indices(rows: list, count: int):
    used = set()
    for row in rows:
        index = row.get("index") if isinstance(row, dict) else None
        if type(index) is not int or not 0 <= index < count or index in used:
            raise FaqProviderError("invalid_response")
        used.add(index)
        yield index, row


def _vectors(response: httpx.Response, count: int) -> list[list[float]]:
    payload, rows = _rows(response, "data")
    if payload.get("model") != EMBEDDING_MODEL or len(rows) != count:
        raise FaqProviderError("invalid_response")
    vectors = {}
    for index, row in _indices(rows, count):
        vector = row.get("embedding")
        if not isinstance(vector, list) or len(vector) != EMBEDDING_DIMENSIONS:
            raise FaqProviderError("invalid_response")
        vectors[index] = [_number(value) for value in vector]
    return [vectors[index] for index in range(count)]


def _texts(values: Sequence[str], limit: int) -> list[str]:
    if isinstance(values, str) or not 1 <= len(values) <= limit:
        raise ValueError("Invalid FAQ text batch size")
    if any(not isinstance(value, str) or not value.strip() for value in values):
        raise ValueError("Expected nonempty FAQ text")
    return list(values)


class SiliconFlowFaqClient:
    def __init__(self, api_key: str, *, transport: httpx.MockTransport | None = None):
        if not api_key.strip():
            raise ValueError("SiliconFlow FAQ credentials are missing")
        self._options = {
            "base_url": PROVIDER_URL,
            "headers": {"Authorization": f"Bearer {api_key.strip()}"},
            "transport": transport,
            "timeout": REQUEST_TIMEOUT,
            "trust_env": False,
            "follow_redirects": False,
        }

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        inputs = _texts(texts, EMBEDDING_BATCH_SIZE)
        try:
            with httpx.Client(**self._options) as client:
                response = client.post("/v1/embeddings", json={"model": EMBEDDING_MODEL, "input": inputs})
        except httpx.TimeoutException:
            raise FaqProviderError("timeout") from None
        except (httpx.HTTPError, ValueError):
            raise FaqProviderError("unavailable") from None
        return _vectors(response, len(inputs))

    async def _post(self, path: str, payload: dict) -> httpx.Response:
        try:
            async with httpx.AsyncClient(**self._options) as client:
                return await client.post(path, json=payload)
        except httpx.TimeoutException:
            raise FaqProviderError("timeout") from None
        except (httpx.HTTPError, ValueError):
            raise FaqProviderError("unavailable") from None

    async def async_embed(self, text: str) -> list[float]:
        inputs = _texts([text], 1)
        response = await self._post("/v1/embeddings", {"model": EMBEDDING_MODEL, "input": inputs})
        return _vectors(response, 1)[0]

    async def rerank(self, question: str, documents: Sequence[str], *, top_n: int) -> list[RankedIndex]:
        _texts([question], 1)
        inputs = _texts(documents, 40)
        if type(top_n) is not int or not 1 <= top_n <= 10:
            raise ValueError("Invalid FAQ rerank result limit")
        response = await self._post(
            "/v1/rerank",
            {"model": RERANK_MODEL, "query": question, "documents": inputs,
             "top_n": top_n, "return_documents": False},
        )
        _, rows = _rows(response, "results")
        if len(rows) > min(top_n, len(inputs)):
            raise FaqProviderError("invalid_response")
        ranked = []
        for index, row in _indices(rows, len(inputs)):
            score = _number(row.get("relevance_score"))
            if not 0 <= score <= 1:
                raise FaqProviderError("invalid_response")
            ranked.append(RankedIndex(index, score))
        return ranked
