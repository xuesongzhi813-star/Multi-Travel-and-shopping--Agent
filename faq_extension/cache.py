"""Short-lived cache for validated public FAQ evidence."""

import asyncio

from pydantic import ValidationError
from redis.asyncio import Redis
from redis.exceptions import RedisError

from .contracts import FaqSearchResult


TTL_SECONDS = 900
REDIS_BUDGET = 0.2


class FaqEvidenceCache:
    def __init__(self, redis: Redis, *, namespace: str = "travel:faq:p9"):
        self.redis = redis
        self.namespace = namespace

    async def get(self, key: str) -> FaqSearchResult | None:
        try:
            async with asyncio.timeout(REDIS_BUDGET):
                raw = await self.redis.get(f"{self.namespace}:{key}")
            if not isinstance(raw, (str, bytes)) or len(raw) > 160_000:
                return None
            result = FaqSearchResult.model_validate_json(raw)
        except (RedisError, TimeoutError, ValidationError):
            return None
        return result if result.degradation is None and result.status in {"ok", "no_evidence"} else None

    async def put(self, key: str, result: FaqSearchResult) -> None:
        if result.degradation is not None or result.status not in {"ok", "no_evidence"}:
            return
        try:
            async with asyncio.timeout(REDIS_BUDGET):
                await self.redis.set(f"{self.namespace}:{key}", result.model_dump_json(), ex=TTL_SECONDS)
        except (RedisError, TimeoutError):
            pass
