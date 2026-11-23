from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, select

from customer_support_chat.app.business.database import migrate_business
from customer_support_chat.app.business.demo_seed import DEMO_USERS, seed_demo
from customer_support_chat.app.business.repository import BusinessRepository
from customer_support_chat.app.tooling.audit import PostgresAuditSink
from customer_support_chat.app.tooling.executor import ToolExecutionRecord
from test_business_migrations import BusinessDatabaseTest


class PostgresAuditTests(BusinessDatabaseTest):
    async def test_migrates_existing_business_schema(self):
        def downgrade(connection):
            config = Config(Path(__file__).resolve().parents[1] / "alembic.ini")
            config.attributes.update(connection=connection, business_schema=self.settings.schema)
            command.downgrade(config, "0001_business")

        async with self.engine.begin() as db:
            await db.run_sync(downgrade)
        await seed_demo(self.engine, self.settings.schema, ("test-a-12345678", "test-b-12345678"))
        repository = BusinessRepository(self.engine, self.settings.schema)
        before = await repository.list_itineraries(DEMO_USERS[0][0])
        self.assertTrue(before)
        await migrate_business(self.engine, self.settings.schema)
        self.assertEqual(await repository.list_itineraries(DEMO_USERS[0][0]), before)
        async with self.engine.connect() as db:
            foreign_keys = await db.run_sync(
                lambda conn: inspect(conn).get_foreign_keys("tool_audit_logs", schema=self.settings.schema),
            )
        self.assertEqual(foreign_keys, [])

    async def test_postgres_audits_all_outcomes_and_keeps_first_duplicate(self):
        sink = PostgresAuditSink(self.engine, self.settings.schema)
        run_id = uuid4()
        statuses = ("ok", "invalid_arguments", "permission_denied", "not_found", "timeout", "tool_error")
        def record(index, status, retry_count):
            return ToolExecutionRecord(
                run_id, uuid4(), None, str(index), "lookup_flight", "builtin", status,
                None, {}, {}, retry_count, 12, datetime.now(timezone.utc),
            )
        for index, status in enumerate(statuses):
            await sink.write(record(index, status, int(index == 4)))
        await sink.write(record(4, "ok", 9))
        audit = self.db.tool_audit_logs
        async with self.engine.connect() as db:
            rows = (await db.execute(select(audit).order_by(audit.c.tool_call_id))).mappings().all()
        self.assertEqual(tuple(row["status"] for row in rows), statuses)
        self.assertEqual(rows[4]["retry_count"], 1)
        self.assertTrue(all(row["arguments"] == {} and row["result_summary"] == {} for row in rows))
        self.assertNotIn("user_id", audit.c)
