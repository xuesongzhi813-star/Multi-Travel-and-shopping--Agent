"""Initialize and operate the separately scoped parent graph checkpoints."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
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

from customer_support_chat.app.business.booking import BookingOwner, BookingScope, BookingSelection, ProposalConflict, ProposalNotFound, ProposalView
from customer_support_chat.app.business.booking_repository import BookingRepository
from customer_support_chat.app.tooling.booking import ApprovedBookingExecutor
from customer_support_chat.app.business.database import DatabaseSettings, connection_kwargs, local_value, validate_schema
from .hotel_handoff import HotelOption
from .parent_graph import BOOKING_NODE, FlightLookup, GraphResult, HotelSearch, MessageLoader, build_parent_graph


ROLE_GRAPH_TIMEOUT = 40


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


@dataclass(frozen=True)
class BookingGraphState:
    action_id: UUID | None
    awaiting_resolution: bool


class GraphRuntime:
    def __init__(
        self, saver: AsyncPostgresSaver, load_message: MessageLoader, *,
        flight_lookup: FlightLookup | None = None, hotel_search: HotelSearch | None = None,
        booking_repository: BookingRepository | None = None, booking_executor: ApprovedBookingExecutor | None = None,
    ):
        self.saver = saver
        self.load_message = load_message
        self.flight_lookup = flight_lookup
        self.hotel_search = hotel_search
        self.booking_repository = booking_repository
        self.booking_executor = booking_executor
        self.graph: CompiledStateGraph = build_parent_graph(
            load_message, saver, flight_lookup=flight_lookup, hotel_search=hotel_search,
            booking_execute=self._resolve_booking if booking_executor is not None else None,
        )

    async def _resolve_booking(self, scope, request_id):
        return await self.booking_executor.execute(scope, request_id=request_id)

    def _attached(self, connection: AsyncConnection) -> "GraphRuntime":
        return GraphRuntime(
            AsyncPostgresSaver(connection, serde=CheckpointJsonSerializer()), self.load_message,
            flight_lookup=self.flight_lookup, hotel_search=self.hotel_search,
            booking_repository=self.booking_repository, booking_executor=self.booking_executor,
        )

    async def read_booking(self, thread_id: UUID, *, connection: AsyncConnection | None = None) -> BookingGraphState:
        runtime = self._attached(connection) if connection is not None else self
        try:
            async with asyncio.timeout(12):
                saved = await runtime.graph.aget_state({"configurable": {"thread_id": str(thread_id)}})
                action = saved.values.get("booking_action_id")
                return BookingGraphState(UUID(action) if action is not None else None, BOOKING_NODE in saved.next)
        except (Error, TimeoutError, ValueError) as error:
            raise GraphUnavailable("Could not read booking checkpoint") from error

    async def start_proposal(
        self, owner: BookingOwner, selection: BookingSelection, *, connection: AsyncConnection,
    ) -> ProposalView:
        runtime = self._attached(connection)
        repository = self.booking_repository
        if repository is None or self.booking_executor is None:
            raise GraphUnavailable("Booking is not configured")
        reused = await repository.find_proposal(owner, selection, connection=connection)
        if reused is not None:
            return reused
        if (await runtime.read_booking(owner.thread_id)).awaiting_resolution:
            raise ProposalConflict("proposal_already_pending")
        completed = await runtime.read(owner.thread_id)
        if completed is None or completed["status"] != "hotel_results" or completed["message_id"] != str(selection.message_id):
            raise ProposalConflict("stale_recommendation")
        options = completed.get("hotel_options", [])
        if not 0 <= selection.option_index < len(options):
            raise ProposalConflict("invalid_recommendation")
        option = HotelOption.from_json(options[selection.option_index])
        proposal = await repository.create_proposal(connection, owner, selection, option)
        try:
            async with asyncio.timeout(12):
                await runtime.graph.ainvoke(
                    {"user_id": str(owner.user_id), "booking_action_id": str(proposal.action_id)},
                    {"configurable": {"thread_id": str(owner.thread_id)}},
                )
        except (Error, TimeoutError, TypeError, ValueError) as error:
            raise GraphUnavailable("Could not pause booking") from error
        state = await runtime.read_booking(owner.thread_id)
        if state != BookingGraphState(proposal.action_id, True):
            raise GraphUnavailable("Booking pause was not saved")
        return proposal

    async def resume_booking(
        self, scope: BookingScope, *, connection: AsyncConnection, request_id: UUID,
    ) -> ProposalView:
        runtime = self._attached(connection)
        repository = self.booking_repository
        if repository is None or self.booking_executor is None:
            raise GraphUnavailable("Booking is not configured")
        proposal = await repository.get_proposal(scope, connection=connection)
        if proposal is None:
            raise ProposalNotFound()
        state = await runtime.read_booking(scope.owner.thread_id)
        if state.action_id != scope.action_id or not state.awaiting_resolution:
            if proposal.status in {"booked", "rejected", "expired", "stale"}:
                return proposal
            raise ProposalConflict("booking_not_suspended")
        if proposal.status == "awaiting_decision":
            raise ProposalConflict("decision_required")
        try:
            async with asyncio.timeout(18):
                await runtime.graph.ainvoke(None, {"configurable": {
                    "thread_id": str(scope.owner.thread_id), "booking_request_id": str(request_id),
                }})
        except (Error, TimeoutError, DBAPIError, DatabasePoolTimeout) as error:
            raise GraphUnavailable("Could not finish booking checkpoint") from error
        saved = await runtime.read_booking(scope.owner.thread_id)
        result = await repository.get_proposal(scope, connection=connection)
        if saved.awaiting_resolution or result is None:
            raise GraphUnavailable("Booking resolution was not saved")
        return result

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
            return await self._attached(connection)._invoke(user_id, thread_id, message_id)
        return await self._invoke(user_id, thread_id, message_id)

    async def _invoke(self, user_id: UUID, thread_id: UUID, message_id: UUID) -> GraphResult:
        try:
            async with asyncio.timeout(ROLE_GRAPH_TIMEOUT if self.hotel_search is not None else 20):
                prior = await self.read(thread_id)
                await self.graph.ainvoke(
                    {
                        "user_id": str(user_id), "message_id": str(message_id), "booking_action_id": None,
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
    booking_repository: BookingRepository | None = None, booking_executor: ApprovedBookingExecutor | None = None,
) -> AsyncIterator[GraphRuntime]:
    pool = AsyncConnectionPool(
        kwargs=checkpoint_kwargs(settings, schema), open=False,
        min_size=0, max_size=4, timeout=5,
    )
    async with pool:
        saver = AsyncPostgresSaver(pool, serde=CheckpointJsonSerializer())
        yield GraphRuntime(
            saver, load_message, flight_lookup=flight_lookup, hotel_search=hotel_search,
            booking_repository=booking_repository, booking_executor=booking_executor,
        )
