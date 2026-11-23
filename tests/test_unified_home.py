import unittest
from uuid import UUID

from httpx import ASGITransport, AsyncClient

from web_app.app.runtime import create_app, get_repository


class UnifiedHomeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        app = create_app()
        app.dependency_overrides[get_repository] = lambda: None
        self.client = AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost")
        self.addAsyncCleanup(self.client.aclose)

    async def test_home_serves_browser_application(self):
        result = await self.client.get("/")
        self.assertEqual(result.status_code, 200)
        self.assertIn("text/html", result.headers["content-type"])
        self.assertIn("旅行客服", result.text)
        self.assertIn("/api/tools/flight", result.text)
        self.assertIn("/api/tools/airport", result.text)
        self.assertIn("hotel_options", result.text)
        self.assertIn("演示总价", result.text)

    async def test_health_liveness_needs_no_database(self):
        result = await self.client.get("/health/live")
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json(), {"status": "ok"})
        self.assertEqual(result.headers["cache-control"], "no-store")

    async def test_error_response_carries_safe_correlation_id(self):
        result = await self.client.post(
            "/api/auth/login", json={"extra": "private-payload"}, headers={"X-Request-ID": "untrusted"},
        )
        self.assertEqual(result.status_code, 422)
        correlation = result.headers["X-Request-ID"]
        self.assertEqual(str(UUID(correlation)), correlation)
        self.assertEqual(result.json(), {
            "detail": "Invalid request", "code": "invalid_request", "request_id": correlation,
        })

    async def test_home_exposes_two_step_demo_booking_controls(self):
        result = await self.client.get("/")
        for fragment in ("hotel-proposals", "/approvals/", "发起演示预订", "确认预订", "拒绝", "继续处理", 'id="booking"'):
            self.assertIn(fragment, result.text, fragment)
