import unittest
from httpx import ASGITransport, AsyncClient

from web_app.app.runtime import create_app


class UnifiedHomeTests(unittest.IsolatedAsyncioTestCase):
    async def test_home_serves_browser_application(self):
        async with AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://localhost") as client:
            result = await client.get("/")
        self.assertEqual(result.status_code, 200)
        self.assertIn("text/html", result.headers["content-type"])
        self.assertIn("旅行客服", result.text)
        self.assertIn("/api/tools/flight", result.text)
        self.assertIn("/api/tools/airport", result.text)
