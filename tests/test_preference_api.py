from unittest.mock import patch
from uuid import uuid4

from httpx import ASGITransport, AsyncClient
from psycopg import OperationalError
from sqlalchemy.schema import DropSchema, DropTable

from customer_support_chat.app.business.demo_seed import seed_demo
from customer_support_chat.app.business.preference_repository import PreferenceRepository
from customer_support_chat.app.workflow.checkpoints import prepare_checkpoints
from test_business_migrations import BusinessDatabaseTest
from test_business_seed import PASSWORDS
from web_app.app.runtime import create_app


PATH = "/api/me/preferences"
EMPTY = {"room_type": None, "area": None, "max_total_chf_minor": None}


class PreferenceApiTests(BusinessDatabaseTest):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await seed_demo(self.engine, self.settings.schema, PASSWORDS)
        self.checkpoint_schema = f"test_graph_{uuid4().hex}"
        self.addAsyncCleanup(self.remove_checkpoint_schema)
        await prepare_checkpoints(self.settings, self.checkpoint_schema)
        app = create_app(self.settings, checkpoint_schema=self.checkpoint_schema)
        await self.enterAsyncContext(app.router.lifespan_context(app))
        transport = ASGITransport(app=app, raise_app_exceptions=False)
        self.a = await self.enterAsyncContext(AsyncClient(transport=transport, base_url="http://127.0.0.1"))
        self.b = await self.enterAsyncContext(AsyncClient(transport=transport, base_url="http://127.0.0.1"))

    async def remove_checkpoint_schema(self):
        async with self.engine.begin() as connection:
            await connection.execute(DropSchema(self.checkpoint_schema, cascade=True, if_exists=True))

    async def login(self, client, index):
        response = await client.post("/api/auth/login", json={
            "username": ("demo-a", "demo-b")[index], "password": PASSWORDS[index],
        })
        self.assertEqual(response.status_code, 204)

    async def get_preferences(self, client):
        response = await client.get(PATH)
        self.assertEqual(response.status_code, 200)
        return response.json()

    async def test_requires_authentication_and_isolates_accounts(self):
        for method, options in (("GET", {}), ("PATCH", {"json": {"area": "city"}}), ("DELETE", {})):
            self.assertEqual((await self.a.request(method, PATH, **options)).status_code, 401)
        await self.login(self.a, 0)
        await self.login(self.b, 1)
        saved = {"room_type": "standard", "area": "city", "max_total_chf_minor": 40000}
        response = await self.a.patch(PATH, json=saved)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), saved)
        self.assertEqual(await self.get_preferences(self.a), saved)
        self.assertEqual(await self.get_preferences(self.b), EMPTY)

    async def test_partial_patch_null_and_empty_delete(self):
        await self.login(self.a, 0)
        stored = await self.a.patch(PATH, json={"area": "airport", "max_total_chf_minor": 1})
        self.assertEqual(stored.status_code, 200)
        cleared = await self.a.patch(PATH, json={"area": None})
        self.assertEqual(cleared.status_code, 200)
        self.assertEqual(cleared.json(), EMPTY | {"max_total_chf_minor": 1})
        for _ in range(2):
            response = await self.a.delete(PATH)
            self.assertEqual(response.status_code, 204)
            self.assertEqual(response.content, b"")
        self.assertEqual(await self.get_preferences(self.a), EMPTY)

    async def test_invalid_patch_has_no_side_effect(self):
        await self.login(self.a, 0)
        self.assertEqual((await self.a.patch(PATH, json={"area": "city"})).status_code, 200)
        for invalid in ({}, {"area": "airport", "user_id": str(uuid4())},
                        *({"max_total_chf_minor": value} for value in (True, "40000", 40000.0, 0, 1000001)),
                        {"room_type": "suite"}, {"area": "suburb"}):
            with self.subTest(invalid=invalid):
                self.assertEqual((await self.a.patch(PATH, json=invalid)).status_code, 422)
        self.assertEqual(await self.get_preferences(self.a), EMPTY | {"area": "city"})

    async def test_unavailable_and_readiness(self):
        await self.login(self.a, 0)
        self.assertEqual(await self.get_preferences(self.a), EMPTY)
        with patch.object(PreferenceRepository, "get", side_effect=OperationalError("SELECT internal diagnostic")):
            response = await self.a.get(PATH)
        self.assertEqual((response.status_code, response.json()["code"]), (503, "preferences_unavailable"))
        self.assertNotIn("internal diagnostic", response.text)
        async with self.engine.begin() as connection:
            await connection.execute(DropTable(self.db.user_preferences))
        readiness = await self.a.get("/health/ready")
        self.assertEqual(readiness.status_code, 503)
        self.assertFalse(readiness.json()["dependencies"]["business_database"])
