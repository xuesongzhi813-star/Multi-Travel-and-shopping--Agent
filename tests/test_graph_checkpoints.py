import unittest
from uuid import uuid4

from sqlalchemy import inspect
from sqlalchemy.schema import DropSchema

from customer_support_chat.app.workflow.checkpoints import (
    CheckpointJsonSerializer, GraphUnavailable, open_graph_runtime, prepare_checkpoints,
)
from test_business_migrations import BusinessDatabaseTest


class CheckpointSerializerTests(unittest.TestCase):
    def test_checkpoint_serializer_uses_plain_values_only(self):
        serializer = CheckpointJsonSerializer()
        value = {"type": "plain", "nested": [None, True, 4, "旅行"]}
        kind, encoded = serializer.dumps_typed(value)
        self.assertEqual(kind, "travel-json-v1")
        self.assertEqual(serializer.loads_typed((kind, encoded)), value)
        with self.assertRaises(ValueError):
            serializer.loads_typed(("unknown", encoded))


class CheckpointTests(BusinessDatabaseTest):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.schema = f"test_p3_{uuid4().hex}"
        self.addAsyncCleanup(self.remove_checkpoint_schema)

    async def remove_checkpoint_schema(self):
        async with self.engine.begin() as connection:
            await connection.execute(DropSchema(self.schema, cascade=True, if_exists=True))

    async def test_setup_twice_in_separate_schema(self):
        await prepare_checkpoints(self.settings, self.schema)
        await prepare_checkpoints(self.settings, self.schema)
        async with self.engine.connect() as conn:
            checkpoint_tables, business_tables = await conn.run_sync(lambda db: (
                inspect(db).get_table_names(schema=self.schema),
                inspect(db).get_table_names(schema=self.settings.schema),
            ))
        self.assertIn("checkpoints", checkpoint_tables)
        self.assertNotIn("checkpoints", business_tables)

    async def test_reopened_saver_reuses_completed_state(self):
        content = "synthetic input that must stay out of checkpoint storage"
        async def load_message(user_id, thread_id, message_id):
            return content

        await prepare_checkpoints(self.settings, self.schema)
        user, thread, first, second = (uuid4() for _ in range(4))
        async with open_graph_runtime(self.settings, self.schema, load_message) as runtime:
            self.assertIsNone(await runtime.read(thread))
            saved = await runtime.invoke(user, thread, first)
            self.assertEqual(saved, {"message_id": str(first), "status": "recorded", "turn_count": 1})
            config = {"configurable": {"thread_id": str(thread)}}
            async for item in runtime.saver.alist(config):
                self.assertNotIn(content, repr(item))
        async with open_graph_runtime(self.settings, self.schema, load_message) as runtime:
            self.assertEqual(await runtime.read(thread), saved)
            self.assertEqual((await runtime.invoke(user, thread, second))["turn_count"], 2)

    async def test_partial_checkpoint_is_not_a_completed_turn(self):
        first = uuid4()
        async def load_message(user_id, thread_id, message_id):
            return "查我的航班" if message_id == first else None

        await prepare_checkpoints(self.settings, self.schema)
        user, thread = uuid4(), uuid4()
        async with open_graph_runtime(self.settings, self.schema, load_message) as runtime:
            saved = await runtime.invoke(user, thread, first)
            with self.assertRaises(GraphUnavailable):
                await runtime.invoke(user, thread, uuid4())
            self.assertEqual(await runtime.read(thread), saved)
