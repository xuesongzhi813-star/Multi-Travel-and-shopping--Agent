"""Use a bounded transport for the single live DeepSeek validation."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
import json

import httpx
from langchain_deepseek import ChatDeepSeek

from .role import MODEL_REQUEST_LIMIT, ModelRequestBudgetExceeded


API_BASE = "https://api.deepseek.com"
API_URL = f"{API_BASE}/chat/completions"
MODEL_NAME = "deepseek-flash"
MAX_OUTPUT_TOKENS = 384
REQUEST_TIMEOUT = 15
MAX_PAYLOAD_BYTES = 16_384
USAGE_FIELDS = ("prompt_tokens", "completion_tokens", "total_tokens")


@dataclass
class RequestObservation:
    returned_tool_ids: tuple[str, ...]
    status: int | None = None
    calls: tuple[tuple[str, str, str], ...] = ()
    usage: dict[str, int] = field(default_factory=dict)


class RequestBudgetTransport(httpx.AsyncBaseTransport):
    def __init__(self, inner: httpx.AsyncBaseTransport, limit: int = MODEL_REQUEST_LIMIT):
        if not 1 <= limit <= MODEL_REQUEST_LIMIT:
            raise ValueError("Invalid request budget")
        self._inner = inner
        self._limit = limit
        self.observations: list[RequestObservation] = []

    @property
    def attempts(self) -> int:
        return len(self.observations)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if self.attempts >= self._limit:
            raise ModelRequestBudgetExceeded("DeepSeek request budget exhausted")
        if request.method != "POST" or str(request.url) != API_URL or len(request.content) > MAX_PAYLOAD_BYTES:
            raise httpx.RequestError("Invalid DeepSeek validation request", request=request)
        payload = json.loads(request.content)
        if (payload.get("model") != MODEL_NAME or payload.get("max_tokens") != MAX_OUTPUT_TOKENS
                or payload.get("thinking") != {"type": "disabled"} or payload.get("stream")):
            raise httpx.RequestError("DeepSeek validation settings differ from approved bounds", request=request)
        observation = RequestObservation(tuple(
            message["tool_call_id"] for message in payload["messages"] if message["role"] == "tool"
        ))
        self.observations.append(observation)
        response = None
        try:
            async with asyncio.timeout(REQUEST_TIMEOUT):
                response = await self._inner.handle_async_request(request)
                observation.status = response.status_code
                await response.aread()
        except TimeoutError as error:
            raise httpx.ReadTimeout("DeepSeek validation request timed out", request=request) from error
        finally:
            if response is not None:
                await response.aclose()
        if response.is_success:
            self._record_completion(response, observation)
        return response

    @staticmethod
    def _record_completion(response: httpx.Response, observation: RequestObservation) -> None:
        try:
            body = response.json()
            usage = body.get("usage") or {}
            observation.usage = {
                key: usage[key] for key in USAGE_FIELDS if type(usage.get(key)) is int and usage[key] >= 0
            }
            calls = body["choices"][0]["message"].get("tool_calls") or []
            observation.calls = tuple(
                (item["id"], item["function"]["name"], json.loads(item["function"]["arguments"])["reference"])
                for item in calls
            )
        except (ValueError, TypeError, IndexError, KeyError, AttributeError):
            # The SDK still receives the response and handles malformed payloads.
            return

    def usage_totals(self) -> dict[str, int]:
        usage = [item.usage for item in self.observations if item.usage]
        return {
            "reported_responses": len(usage),
            **{field: sum(item[field] for item in usage)
               for field in USAGE_FIELDS if usage and all(field in item for item in usage)},
        }

    def sequential_lookup_verified(self, evidence_id: str) -> bool:
        if len(self.observations) != 3:
            return False
        first, second, third = self.observations
        if any(item.status != 200 or len(item.calls) != 1 for item in self.observations):
            return False
        first_id, first_name, first_ref = first.calls[0]
        second_id, second_name, second_ref = second.calls[0]
        _, third_name, third_ref = third.calls[0]
        return (
            first_id == evidence_id
            and first_name == second_name == "lookup_flight"
            and (first_ref, second_ref, third_ref) == ("DEMO-A1", "DEMO-A2", "DEMO-A1")
            and third_name == "HandoffCandidate"
            and first_id in second.returned_tool_ids
            and second_id not in second.returned_tool_ids
            and second_id in third.returned_tool_ids
        )

    async def aclose(self) -> None:
        await self._inner.aclose()


@asynccontextmanager
async def open_live_model(
    api_key: str, *, transport: httpx.AsyncBaseTransport | None = None,
) -> AsyncIterator[tuple[ChatDeepSeek, RequestBudgetTransport]]:
    guarded = RequestBudgetTransport(transport or httpx.AsyncHTTPTransport(retries=0, trust_env=False))
    async with httpx.AsyncClient(
        transport=guarded, trust_env=False, follow_redirects=False, timeout=REQUEST_TIMEOUT,
    ) as client:
        yield ChatDeepSeek(
            api_key=api_key,
            base_url=API_BASE,
            model=MODEL_NAME,
            max_tokens=MAX_OUTPUT_TOKENS,
            max_retries=0,
            timeout=REQUEST_TIMEOUT,
            temperature=0,
            use_responses_api=False,
            extra_body={"thinking": {"type": "disabled"}},
            http_async_client=client,
        ), guarded
