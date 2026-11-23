"""Write one audit record independently of the caller's business transaction."""

from dataclasses import asdict

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine

from customer_support_chat.app.business.schema import tables
from .executor import ToolExecutionRecord


class PostgresAuditSink:
    def __init__(self, engine: AsyncEngine, schema: str):
        self._engine = engine
        self._table = tables(schema).tool_audit_logs

    async def write(self, record: ToolExecutionRecord) -> None:
        table = self._table
        statement = insert(table).values(**asdict(record)).on_conflict_do_nothing(
            index_elements=(table.c.run_id, table.c.tool_call_id),
        )
        async with self._engine.begin() as connection:
            await connection.execute(statement)
