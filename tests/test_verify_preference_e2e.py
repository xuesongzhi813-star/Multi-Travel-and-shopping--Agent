from unittest import TestCase
from uuid import uuid4

from httpx import Client, MockTransport, Response

from scripts.verify_local_e2e import verify_preferences


class PreferenceE2eVerifierTests(TestCase):
    def test_verifier_rejects_missing_preferences_route(self):
        def handle(request):
            if request.method == "POST" and request.url.path == "/api/auth/login":
                return Response(204, headers={"X-Request-ID": str(uuid4())})
            self.assertEqual((request.method, request.url.path), ("GET", "/api/me/preferences"))
            return Response(404)

        with Client(transport=MockTransport(handle), base_url="http://test.local") as client:
            with self.assertRaisesRegex(RuntimeError, "GET /api/me/preferences returned 404"):
                verify_preferences(client, "fixture-a", "fixture-b")
