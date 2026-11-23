"""Initialize and operate the separately scoped parent graph checkpoints."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from hashlib import blake2b
import json
from typing import Any
from uuid import UUID

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph.state import CompiledStateGraph
from psycopg import AsyncConnection, Error, sql
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from sqlalchemy.exc import DBAPIError, TimeoutError as DatabasePoolTimeout

from customer_support_chat.app.business.database import DatabaseSettings, local_value, validate_schema
from .parent_graph import FlightLookup, GraphResult, HotelSearch, MessageLoader, build_parent_graph


class GraphUnavailable(RuntimeError):
    """The outcome of this graph operation could not be confirmed."""


class CheckpointJsonSerializer:
    """Persist JSON values without loading arbitrary Python objects."""

    def dumps(self, value: Any) -> bytes:
        return json.dumps(value, allow_nan=False).encode("utf-8")

    def loads(self, data: bytes) -> Any:
        return json.loads(data)

    def dumps_typed(self, value: Any) -> tuple[str, bytes]:
        return "travel-json-v1", self.dumps(value)

    def loads_typed(self, value: tuple[str, bytes]) -> Any:
        kind, data = value
        if kind != "travel-json-v1":
            raise ValueError("Unexpected checkpoint encoding")
        return self.loads(data)


def validate_checkpoint_schema(business_schema: str, schema: str) -> None:
    validate_schema(schema)
    if schema == business_schema or schema == "information_schema" or schema.startswith("pg_"):
        raise ValueError("invalid checkpoint schema")


def read_checkpoint_schema(business_schema: str) -> str:
    schema = local_value("P3_CHECKPOINT_SCHEMA", "travel_checkpoints")
    validate_checkpoint_schema(business_schema, schema)
    return schema


def connection_kwargs(settings: DatabaseSettings) -> dict[str, Any]:
    return {
        "host": "127.0.0.1", "port": settings.port, "dbname": settings.name,
        "user": settings.user, "password": settings.password, "connect_timeout": 5,
    }


def checkpoint_kwargs(settings: DatabaseSettings, schema: str) -> dict[str, Any]:
    validate_checkpoint_schema(settings.schema, schema)
    return connection_kwargs(settings) | {
        "autocommit": True, "row_factory": dict_row,
        "options": f"-c search_path={schema} -c statement_timeout=8000 -c lock_timeout=5000",
    }


async def prepare_checkpoints(settings: DatabaseSettings, schema: str) -> None:
    lock_id = int.from_bytes(
        blake2b(f"travel-checkpoint-init:{schema}".encode(), digest_size=8).digest(),
        "big", signed=True,
    )
    async with await AsyncConnection.connect(**checkpoint_kwargs(settings, schema)) as conn:
        await conn.execute("SELECT pg_advisory_lock(%s)", (lock_id,))
        await conn.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(schema)))
        await AsyncPostgresSaver(conn, serde=CheckpointJsonSerializer()).setup()


class GraphRuntime:
    def __init__(
        self, saver: AsyncPostgresSaver, load_message: MessageLoader, *,
        flight_lookup: FlightLookup | None = None, hotel_search: HotelSearch | None = None,
    ):
        self.saver = saver
        self.load_message = load_message
        self.flight_lookup = flight_lookup
        self.hotel_search = hotel_search
        self.graph: CompiledStateGraph = build_parent_graph(
            load_message, saver, flight_lookup=flight_lookup, hotel_search=hotel_search,
        )

    async def read(self, thread_id: UUID) -> GraphResult | None:
        try:
            async with asyncio.timeout(12):
                saved = await self.saver.aget_tuple({"configurable": {"thread_id": str(thread_id)}})
        except (Error, TimeoutError, ValueError) as error:
            raise GraphUnavailable("Could not read graph state") from error
        return None if saved is None else saved.checkpoint["channel_values"].get("completed")

    async def invoke(
        self, user_id: UUID, thread_id: UUID, message_id: UUID, *, connection: AsyncConnection | None = None,
    ) -> GraphResult:
        if connection is not None:
            saver = AsyncPostgresSaver(connection, serde=CheckpointJsonSerializer())
            runtime = GraphRuntime(
                saver, self.load_message, flight_lookup=self.flight_lookup, hotel_search=self.hotel_search,
            )
            return await runtime._invoke(user_id, thread_id, message_id)
        return await self._invoke(user_id, thread_id, message_id)

    async def _invoke(self, user_id: UUID, thread_id: UUID, message_id: UUID) -> GraphResult:
        try:
            async with asyncio.timeout(20):
                prior = await self.read(thread_id)
                await self.graph.ainvoke(
                    {
                        "user_id": str(user_id), "message_id": str(message_id),
                        "prior_turn_count": 0 if prior is None else prior["turn_count"],
                    },
                    {"configurable": {"thread_id": str(thread_id)}},
                )
                result = await self.read(thread_id)
        except (Error, TimeoutError, LookupError, ValueError, DBAPIError, DatabasePoolTimeout) as error:
            raise GraphUnavailable("Could not process graph turn") from error
        if result is None or result["message_id"] != str(message_id):
            raise GraphUnavailable("Graph turn was not confirmed")
        return result


@asynccontextmanager
async def open_graph_runtime(
    settings: DatabaseSettings, schema: str, load_message: MessageLoader, *,
    flight_lookup: FlightLookup | None = None, hotel_search: HotelSearch | None = None,
) -> AsyncIterator[GraphRuntime]:
    pool = AsyncConnectionPool(
        kwargs=checkpoint_kwargs(settings, schema), open=False,
        min_size=0, max_size=4, timeout=5,
    )
    async with pool:
        saver = AsyncPostgresSaver(pool, serde=CheckpointJsonSerializer())
        yield GraphRuntime(saver, load_message, flight_lookup=flight_lookup, hotel_search=hotel_search)
