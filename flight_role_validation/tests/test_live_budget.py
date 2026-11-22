import json
import unittest

import httpx
from openai import APIError

from flight_role_validation.live import API_URL, MODEL_NAME, RequestBudgetTransport, open_live_model
from flight_role_validation.role import ModelRequestBudgetExceeded


def completion():
    return {
        "id": "synthetic-response",
        "object": "chat.completion",
        "created": 1,
        "model": MODEL_NAME,
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant", "content": "synthetic response"}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }


class LiveBudgetTests(unittest.IsolatedAsyncioTestCase):
    async def test_budget_counts_failed_sends_and_blocks_fourth(self):
        seen = []

        async def fail(request):
            seen.append(request.url.path)
            raise httpx.ReadTimeout("synthetic timeout", request=request)

        budget = RequestBudgetTransport(httpx.MockTransport(fail))
        async with httpx.AsyncClient(transport=budget) as client:
            for _ in range(3):
                with self.assertRaises(httpx.ReadTimeout):
                    await client.post(API_URL, json={
                        "model": MODEL_NAME, "max_tokens": 384, "thinking": {"type": "disabled"},
                        "messages": [],
                    })
            with self.assertRaises(ModelRequestBudgetExceeded):
                await client.post(API_URL, json={
                    "model": MODEL_NAME, "max_tokens": 384, "thinking": {"type": "disabled"},
                    "messages": [],
                })
        self.assertEqual(budget.attempts, 3)
        self.assertEqual(len(seen), 3)

    async def test_transport_disables_retry_redirect_and_caps_payload(self):
        for failure in (429, 302, "timeout", None):
            with self.subTest(failure=failure):
                requests = []

                async def respond(request):
                    requests.append(request)
                    if failure == "timeout":
                        raise httpx.ReadTimeout("synthetic", request=request)
                    if failure == 302:
                        return httpx.Response(302, headers={"location": "https://redirect.example/"})
                    if failure == 429:
                        return httpx.Response(429, json={"error": {"message": "synthetic"}})
                    return httpx.Response(200, json=completion())

                async with open_live_model("synthetic-not-a-real-key", transport=httpx.MockTransport(respond)) as pair:
                    model, budget = pair
                    if failure is not None:
                        with self.assertRaises(APIError):
                            await model.ainvoke("synthetic request")
                    else:
                        await model.ainvoke("synthetic request")
                        self.assertEqual(budget.usage_totals(), {
                            "reported_responses": 1, "prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15,
                        })
                self.assertEqual((budget.attempts, len(requests)), (1, 1))
                self.assertEqual(str(requests[0].url), API_URL)
                body = json.loads(requests[0].content)
                self.assertEqual(body["max_tokens"], 384)
                self.assertEqual(body["thinking"], {"type": "disabled"})

    async def test_mocked_deepseek_loop_matches_wire_contract(self):
        from flight_role_validation.__main__ import SYNTHETIC_INPUT, synthetic_lookup
        from flight_role_validation.role import run_flight_role

        replies = iter((
            ("first", "lookup_flight", "DEMO-A1"),
            ("second", "lookup_flight", "DEMO-A2"),
            ("last", "HandoffCandidate", "DEMO-A1"),
        ))

        async def reply(request):
            call_id, name, reference = next(replies)
            body = completion()
            body["choices"] = [{"index": 0, "finish_reason": "tool_calls", "message": {
                "role": "assistant", "content": None, "tool_calls": [{
                    "id": call_id, "type": "function",
                    "function": {"name": name, "arguments": json.dumps({"reference": reference})},
                }],
            }}]
            return httpx.Response(200, json=body)

        async with open_live_model("synthetic-not-a-real-key", transport=httpx.MockTransport(reply)) as pair:
            model, budget = pair
            result = await run_flight_role(SYNTHETIC_INPUT, model=model, lookup=synthetic_lookup)
        self.assertEqual(result.status, "verified")
        self.assertEqual(budget.attempts, 3)
        self.assertTrue(budget.sequential_lookup_verified(result.handoff.source_tool_call_id))
        self.assertEqual(budget.usage_totals()["total_tokens"], 45)
