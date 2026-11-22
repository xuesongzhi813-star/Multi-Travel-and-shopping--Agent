"""Serialize authenticated turns across API processes."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from hashlib import blake2b
from uuid import UUID

from psycopg import AsyncConnection

from customer_support_chat.app.business.database import DatabaseSettings
from .checkpoints import checkpoint_kwargs


@asynccontextmanager
async def try_thread_lock(
    settings: DatabaseSettings, checkpoint_schema: str, thread_id: UUID,
) -> AsyncIterator[AsyncConnection | None]:
    key = int.from_bytes(
        blake2b(f"travel-turn:{settings.schema}:{thread_id}".encode(), digest_size=8).digest(),
        "big", signed=True,
    )
    async with await AsyncConnection.connect(**checkpoint_kwargs(settings, checkpoint_schema)) as connection:
        async with connection.transaction():
            cursor = await connection.execute(
                "SELECT pg_try_advisory_xact_lock(%s::bigint) AS acquired", (key,),
            )
            row = await cursor.fetchone()
            yield connection if row["acquired"] else None
