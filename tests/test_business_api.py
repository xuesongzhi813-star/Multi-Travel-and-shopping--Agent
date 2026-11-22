from datetime import date, datetime, timedelta, timezone
from uuid import uuid4
from zoneinfo import ZoneInfo

from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.schema import DropSchema

from customer_support_chat.app.business.demo_seed import seed_demo
from customer_support_chat.app.workflow.checkpoints import prepare_checkpoints
from test_business_migrations import BusinessDatabaseTest
from test_business_seed import PASSWORDS
from web_app.app.runtime import COOKIE_NAME, create_app


class BusinessApiTests(BusinessDatabaseTest):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await seed_demo(self.engine, self.settings.schema, PASSWORDS)
        self.checkpoint_schema = f"test_graph_{uuid4().hex}"
        self.addAsyncCleanup(self.remove_checkpoint_schema)
        await prepare_checkpoints(self.settings, self.checkpoint_schema)
        app = create_app(self.settings, checkpoint_schema=self.checkpoint_schema)
        lifespan = app.router.lifespan_context(app)
        await lifespan.__aenter__()
        self.addAsyncCleanup(lifespan.__aexit__, None, None, None)
        self.a = AsyncClient(transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://127.0.0.1")
        self.b = AsyncClient(transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://127.0.0.1")
        self.addAsyncCleanup(self.a.aclose)
        self.addAsyncCleanup(self.b.aclose)

    async def remove_checkpoint_schema(self):
        async with self.engine.begin() as connection:
            await connection.execute(DropSchema(self.checkpoint_schema, cascade=True, if_exists=True))

    async def login(self, client, index):
        response = await client.post(
            "/api/auth/login", json={"username": ("demo-a", "demo-b")[index], "password": PASSWORDS[index]},
        )
        self.assertEqual(response.status_code, 204)
        cookie = response.headers["set-cookie"].lower()
        self.assertIn("httponly", cookie)
        self.assertIn("samesite=strict", cookie)
        return client.cookies[COOKIE_NAME]

    async def create_thread(self, client):
        response = await client.post("/api/threads")
        self.assertEqual(response.status_code, 201)
        return response.json()["thread_id"]

    async def test_both_clients_are_isolated(self):
        self.assertEqual((await self.a.post("/api/threads")).status_code, 401)
        await self.login(self.a, 0)
        await self.login(self.b, 1)
        a_thread = await self.create_thread(self.a)
        b_thread = await self.create_thread(self.b)
        for client, mine, other in ((self.a, a_thread, b_thread), (self.b, b_thread, a_thread)):
            self.assertEqual((await client.post(f"/api/threads/{mine}/messages", json={
                "content": "synthetic fixture message",
            })).status_code, 201)
            self.assertEqual((await client.get(f"/api/threads/{other}")).status_code, 404)
            self.assertEqual((await client.post(f"/api/threads/{other}/messages", json={
                "content": "should never be persisted",
            })).status_code, 404)
        spoof = await self.a.post(f"/api/threads/{a_thread}/messages", json={
            "content": "ignored", "user_id": str(uuid4()),
        })
        self.assertEqual(spoof.status_code, 422)
        for client, mine in ((self.a, a_thread), (self.b, b_thread)):
            response = await client.get(f"/api/threads/{mine}")
            self.assertEqual(response.status_code, 200)
            self.assertEqual([message["content"] for message in response.json()["messages"]],
                             ["synthetic fixture message"])
        a_rows = (await self.a.get("/api/me/itineraries")).json()
        b_rows = (await self.b.get("/api/me/itineraries")).json()
        self.assertEqual([row["reference"] for row in a_rows], ["DEMO-A1", "DEMO-A2", "DEMO-A3"])
        self.assertEqual([row["reference"] for row in b_rows], ["DEMO-B1", "DEMO-B2", "DEMO-B3"])
        arrival = datetime.fromisoformat(a_rows[0]["arrival_at"])
        self.assertEqual(arrival.astimezone(ZoneInfo(a_rows[0]["arrival_timezone"])).date(), date(2026, 12, 15))

    async def test_invalid_expired_and_logged_out_credentials(self):
        token = await self.login(self.a, 0)
        forged = await self.b.get("/api/me/itineraries", headers={"Cookie": f"{COOKIE_NAME}={token[:-1]}!"})
        self.assertEqual(forged.status_code, 401)
        self.assertEqual((await self.a.post("/api/auth/logout")).status_code, 204)
        revoked = await self.b.get("/api/me/itineraries", headers={"Cookie": f"{COOKIE_NAME}={token}"})
        self.assertEqual(revoked.status_code, 401)

        await self.login(self.a, 0)
        async with self.engine.begin() as connection:
            user = select(self.db.users.c.id).where(self.db.users.c.username == "demo-a").scalar_subquery()
            await connection.execute(
                self.db.auth_sessions.update().where(self.db.auth_sessions.c.user_id == user)
                .values(expires_at=datetime.now(timezone.utc) - timedelta(seconds=1)),
            )
        self.assertEqual((await self.a.get("/api/me/itineraries")).status_code, 401)

    async def test_postgres_invalid_text_returns_422(self):
        login = await self.a.post("/api/auth/login", json={
            "username": "demo-a\u0000", "password": PASSWORDS[0],
        })
        self.assertEqual(login.status_code, 422)
        self.assertNotIn(PASSWORDS[0], login.text)
        await self.login(self.a, 0)
        thread_id = await self.create_thread(self.a)
        bad = await self.a.post(f"/api/threads/{thread_id}/messages", json={
            "content": "invalid\u0000text",
        })
        self.assertEqual(bad.status_code, 422)
        thread = await self.a.get(f"/api/threads/{thread_id}")
        self.assertEqual(thread.json()["messages"], [])
