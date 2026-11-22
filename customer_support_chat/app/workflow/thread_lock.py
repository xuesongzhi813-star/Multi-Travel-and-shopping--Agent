"""Serialize authenticated turns across API processes."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from hashlib import blake2b
from uuid import UUID

from psycopg import AsyncConnection

from customer_support_chat.app.business.database import DatabaseSettings
from .checkpoints import connection_kwargs


@asynccontextmanager
async def try_thread_lock(settings: DatabaseSettings, thread_id: UUID) -> AsyncIterator[bool]:
    key = int.from_bytes(
        blake2b(f"travel-turn:{settings.schema}:{thread_id}".encode(), digest_size=8).digest(),
        "big", signed=True,
    )
    async with await AsyncConnection.connect(
        **connection_kwargs(settings), options="-c statement_timeout=5000",
    ) as connection:
        cursor = await connection.execute("SELECT pg_try_advisory_xact_lock(%s::bigint)", (key,))
        row = await cursor.fetchone()
        yield bool(row[0])
