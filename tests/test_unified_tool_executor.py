import asyncio
import unittest
from uuid import uuid4

from pydantic import BaseModel, ConfigDict

from customer_support_chat.app.tooling.executor import (
    RetryableToolError, ToolContext, ToolDefinition, ToolExecutor, ToolRegistry,
)


class Arguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    code: str


class Result(BaseModel):
    model_config = ConfigDict(strict=True)
    city: str


class Sink:
    def __init__(self):
        self.rows = []

    async def write(self, record):
        self.rows.append(record)


class ToolExecutorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.context = ToolContext(uuid4(), "demo", uuid4())
        self.sink = Sink()
        self.calls = []

    def executor(self, handler, *, timeout=0.05, audit_timeout=0.02):
        registry = ToolRegistry()
        registry.register(ToolDefinition(
            "airport", "Find city", Arguments, Result, "builtin", frozenset({"demo"}),
            handler, audit_argument_fields=frozenset({"code"}),
        ))
        return ToolExecutor(registry, self.sink, timeout=timeout, audit_timeout=audit_timeout)

    async def test_terminal_statuses_and_projection(self):
        async def handle(context, args, call_id):
            if args.code == "missing":
                return None
            if args.code == "broken":
                raise RuntimeError("private raw error")
            if args.code == "slow":
                await asyncio.sleep(1)
            return {"city": "Zürich", "private": "hidden"}

        executor = self.executor(handle)
        for index, (code, status) in enumerate(
            (("ZRH", "ok"), ("missing", "not_found"), ("broken", "tool_error"), ("slow", "timeout")),
        ):
            result = await executor.execute(self.context, "airport", {"code": code}, str(index))
            self.assertEqual((result.status, result.tool_call_id), (status, str(index)))
            self.assertEqual(result.data, {"city": "Zürich"} if status == "ok" else None)
        self.assertNotIn("private raw error", repr(self.sink.rows))

    async def test_permission_and_invalid_input_never_execute(self):
        async def unexpected(*args):
            self.fail("unauthorized or invalid request reached handler")

        executor = self.executor(unexpected)
        other = ToolContext(uuid4(), "other", self.context.run_id)
        self.assertEqual(executor.registry.list_for_role("other"), ())
        self.assertEqual(len(executor.registry.list_for_role("demo")), 1)
        for index, (context, name, arguments, status) in enumerate((
            (other, "airport", {"code": "ZRH"}, "permission_denied"),
            (self.context, "private tool", {"secret": "secret text"}, "permission_denied"),
            (self.context, "airport", {"code": "ZRH", "secret": "secret text"}, "invalid_arguments"),
        )):
            self.assertEqual((await executor.execute(context, name, arguments, str(index))).status, status)
        self.assertTrue(all(not row.arguments for row in self.sink.rows))
        self.assertNotIn("private tool", repr(self.sink.rows))
        self.assertNotIn("secret text", repr(self.sink.rows))

    async def test_transient_retry_shares_deadline(self):
        async def handle(context, args, call_id):
            self.calls.append(call_id)
            if len(self.calls) == 1:
                await asyncio.sleep(0.3)
                raise RetryableToolError("not recorded")
            await asyncio.sleep(0.3)
            return {"city": "Zürich"}

        result = await self.executor(handle, timeout=0.5).execute(self.context, "airport", {"code": "ZRH"}, "same")
        self.assertEqual(result.status, "timeout")
        self.assertEqual(self.calls, ["same", "same"])
        self.assertEqual(len(self.sink.rows), 1)
        self.assertEqual(self.sink.rows[0].retry_count, 1)

    async def test_audit_failure_leaves_read_result_observable(self):
        class SlowSink:
            async def write(self, record):
                await asyncio.sleep(1)

        self.sink = SlowSink()

        async def handle(*args):
            return {"city": "Zürich"}

        with self.assertLogs("customer_support_chat.app.tooling.executor", "WARNING") as logs:
            result = await self.executor(handle).execute(self.context, "airport", {"code": "PRIVATE"}, "id")
        self.assertEqual(result.status, "ok")
        self.assertIn(str(self.context.run_id), repr(logs.output))
        self.assertNotIn("PRIVATE", repr(logs.output))
