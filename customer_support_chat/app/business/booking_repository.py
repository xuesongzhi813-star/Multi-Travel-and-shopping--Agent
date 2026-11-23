"""Ownership-scoped PostgreSQL storage for demo hotel approvals."""

from contextlib import asynccontextmanager
from dataclasses import fields
from uuid import uuid4

from psycopg import AsyncConnection, sql
from psycopg.errors import UniqueViolation
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from customer_support_chat.app.workflow.hotel_handoff import HotelOption

from .booking import BookingDecision, BookingOwner, BookingResolution, BookingScope, BookingSelection, ProposalConflict, ProposalNotFound, ProposalView
from .database import DatabaseSettings, connection_kwargs, validate_schema


class BookingRepository:
    def __init__(self, settings: DatabaseSettings):
        validate_schema(settings.schema)
        self.settings = settings
        self.proposals = sql.Identifier(settings.schema, "hotel_booking_proposals")
        self.threads = sql.Identifier(settings.schema, "threads")
        self.messages = sql.Identifier(settings.schema, "messages")
        self.hotels = sql.Identifier(settings.schema, "hotels")
        self.inventory = sql.Identifier(settings.schema, "hotel_inventory")
        self.receipts = sql.Identifier(settings.schema, "hotel_booking_receipts")
        self.bookings = sql.Identifier(settings.schema, "hotel_bookings")

    @asynccontextmanager
    async def _connection(self, provided=None):
        if provided is not None:
            yield provided
            return
        async with await AsyncConnection.connect(
            **connection_kwargs(self.settings), row_factory=dict_row,
            options="-c statement_timeout=8000 -c lock_timeout=5000",
        ) as connection:
            yield connection

    async def _select(self, connection, owner, where, params=(), *, lock=False):
        query = sql.SQL(
            "SELECT p.*, r.booking_id FROM {proposals} p JOIN {threads} t "
            "ON t.id=p.thread_id AND t.user_id=p.user_id "
            "LEFT JOIN {receipts} r ON r.action_id=p.action_id "
            "WHERE p.user_id=%s AND p.thread_id=%s "
        ).format(proposals=self.proposals, threads=self.threads, receipts=self.receipts)
        suffix = sql.SQL(" FOR UPDATE OF p") if lock else sql.SQL("")
        cursor = await connection.execute(query + where + suffix, (owner.user_id, owner.thread_id, *params))
        return await cursor.fetchone()

    @staticmethod
    def _view(row, now=None):
        if row is None:
            return None
        view = {field.name: row.get(field.name) for field in fields(ProposalView)}
        expirable = row["status"] == "awaiting_decision" or (
            row["status"] == "decided" and row["decision"] == "approve"
        )
        if expirable and now is not None and row["expires_at"] <= now:
            view["status"] = "expired"
        return ProposalView(**view)

    @staticmethod
    async def _now(connection):
        return (await (await connection.execute("SELECT clock_timestamp() AS now")).fetchone())["now"]

    async def find_proposal(
        self, owner: BookingOwner, selection: BookingSelection, *, connection: AsyncConnection | None = None,
    ) -> ProposalView | None:
        async with self._connection(connection) as conn:
            row = await self._select(conn, owner, sql.SQL("AND p.source_message_id=%s"), (selection.message_id,))
            if row is not None and row["option_index"] != selection.option_index:
                raise ProposalConflict("recommendation_already_used")
            return self._view(row, await self._now(conn))

    async def get_proposal(self, scope: BookingScope, *, connection: AsyncConnection | None = None) -> ProposalView | None:
        async with self._connection(connection) as conn:
            row = await self._select(conn, scope.owner, sql.SQL("AND p.action_id=%s"), (scope.action_id,))
            return self._view(row, await self._now(conn))

    async def latest_proposal(self, owner: BookingOwner, *, connection: AsyncConnection | None = None) -> ProposalView | None:
        async with self._connection(connection) as conn:
            row = await self._select(conn, owner, sql.SQL("ORDER BY p.created_at DESC, p.action_id DESC LIMIT 1"))
            return self._view(row, await self._now(conn))

    async def _stock(self, connection, hotel_id, room_type, check_in, check_out, *, lock=False):
        clause = sql.SQL(" FOR UPDATE OF i") if lock else sql.SQL("")
        cursor = await connection.execute(
            sql.SQL(
                "SELECT i.stay_date, i.quantity, i.price_minor, i.currency, h.name FROM {inventory} i "
                "JOIN {hotels} h ON h.id=i.hotel_id WHERE i.hotel_id=%s AND i.room_type=%s "
                "AND i.stay_date>=%s AND i.stay_date<%s ORDER BY i.stay_date"
            ).format(inventory=self.inventory, hotels=self.hotels) + clause,
            (hotel_id, room_type, check_in, check_out),
        )
        return await cursor.fetchall()

    @staticmethod
    def _snapshot(rows):
        return [
            {"date": row["stay_date"].isoformat(), "quantity": row["quantity"],
             "price_minor": row["price_minor"], "currency": row["currency"]}
            for row in rows
        ]

    async def create_proposal(
        self, connection: AsyncConnection, owner: BookingOwner, selection: BookingSelection, option: HotelOption,
    ) -> ProposalView:
        reused = await self.find_proposal(owner, selection, connection=connection)
        if reused is not None:
            return reused
        allowed = await (await connection.execute(sql.SQL(
            "SELECT 1 FROM {messages} m JOIN {threads} t ON t.id=m.thread_id "
            "WHERE m.id=%s AND t.id=%s AND t.user_id=%s"
        ).format(messages=self.messages, threads=self.threads),
            (selection.message_id, owner.thread_id, owner.user_id),
        )).fetchone()
        if allowed is None:
            raise ProposalNotFound()
        nights = (option.check_out - option.check_in).days
        if not 0 <= selection.option_index <= 4 or not 1 <= nights <= 14:
            raise ProposalConflict("invalid_recommendation")
        rows = await self._stock(connection, option.hotel_id, option.room_type, option.check_in, option.check_out)
        if (
            len(rows) != nights or any(row["quantity"] <= 0 or row["currency"] != option.currency for row in rows)
            or sum(row["price_minor"] for row in rows) != option.total_minor
        ):
            raise ProposalConflict("quote_changed")
        try:
            async with connection.transaction():
                row = await (await connection.execute(sql.SQL(
                    "INSERT INTO {} (action_id, user_id, thread_id, source_message_id, option_index, hotel_id, "
                    "hotel_name, room_type, check_in, check_out, total_minor, currency, night_snapshot, status, created_at, expires_at) "
                    "SELECT %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'awaiting_decision',ts,ts+interval '10 minutes' "
                    "FROM (SELECT clock_timestamp() AS ts) clock RETURNING *"
                ).format(self.proposals), (
                    uuid4(), owner.user_id, owner.thread_id, selection.message_id, selection.option_index,
                    option.hotel_id, rows[0]["name"], option.room_type, option.check_in, option.check_out,
                    option.total_minor, option.currency, Jsonb(self._snapshot(rows)),
                ))).fetchone()
        except UniqueViolation as error:
            raise ProposalConflict("proposal_already_pending") from error
        return self._view(row)

    async def record_decision(self, scope: BookingScope, decision: BookingDecision) -> ProposalView:
        async with self._connection() as connection:
            row = await self._select(connection, scope.owner, sql.SQL("AND p.action_id=%s"), (scope.action_id,), lock=True)
            if row is None:
                raise ProposalNotFound()
            if row["decision"] is not None and row["decision"] != decision:
                raise ProposalConflict("decision_already_recorded")
            if row["status"] != "awaiting_decision":
                return self._view(row)
            if row["expires_at"] <= await self._now(connection):
                return self._view(await self._set_status(connection, scope.action_id, "expired"))
            result = await connection.execute(sql.SQL(
                "UPDATE {} SET decision=%s,status='decided' WHERE action_id=%s RETURNING *"
            ).format(self.proposals), (decision, scope.action_id))
            return self._view(await result.fetchone())

    async def _set_status(self, connection, action_id, status):
        cursor = await connection.execute(
            sql.SQL("UPDATE {} SET status=%s WHERE action_id=%s RETURNING *").format(self.proposals),
            (status, action_id),
        )
        return await cursor.fetchone()

    async def resolve_action(self, scope: BookingScope) -> BookingResolution:
        async with self._connection() as connection:
            row = await self._select(connection, scope.owner, sql.SQL("AND p.action_id=%s"), (scope.action_id,), lock=True)
            if row is None:
                raise ProposalNotFound()
            # Read again after acquiring the action row lock. A prior transaction
            # may have committed its receipt while this query was waiting.
            receipt = await (await connection.execute(
                sql.SQL("SELECT booking_id FROM {} WHERE action_id=%s").format(self.receipts), (scope.action_id,),
            )).fetchone()
            if receipt is not None:
                return BookingResolution(self._view(row | receipt), reused_receipt=True)
            if row["status"] in {"rejected", "expired", "stale"}:
                return BookingResolution(self._view(row))
            if row["status"] == "booked":
                raise ProposalConflict("missing_booking_receipt")
            if row["decision"] == "reject":
                return await self._finish(connection, scope.action_id, "rejected")
            if row["expires_at"] <= await self._now(connection):
                return await self._finish(connection, scope.action_id, "expired")
            if row["decision"] is None:
                raise ProposalConflict("decision_required")

            stock = await self._stock(
                connection, row["hotel_id"], row["room_type"], row["check_in"], row["check_out"], lock=True,
            )
            if row["expires_at"] <= await self._now(connection):
                return await self._finish(connection, scope.action_id, "expired")
            if self._snapshot(stock) != row["night_snapshot"] or any(night["quantity"] <= 0 for night in stock):
                return await self._finish(connection, scope.action_id, "stale")

            result = await connection.execute(sql.SQL(
                "UPDATE {} SET quantity=quantity-1 WHERE hotel_id=%s AND room_type=%s AND stay_date>=%s AND stay_date<%s"
            ).format(self.inventory), (row["hotel_id"], row["room_type"], row["check_in"], row["check_out"]))
            if result.rowcount != len(stock):
                raise ProposalConflict("inventory_count_changed")
            booking_id = uuid4()
            await connection.execute(sql.SQL(
                "INSERT INTO {} (action_id,booking_id) VALUES (%s,%s)"
            ).format(self.bookings), (scope.action_id, booking_id))
            await connection.execute(sql.SQL(
                "INSERT INTO {} (action_id,booking_id) VALUES (%s,%s)"
            ).format(self.receipts), (scope.action_id, booking_id))
            done = await self._set_status(connection, scope.action_id, "booked")
            return BookingResolution(self._view(done | {"booking_id": booking_id}))

    async def _finish(self, connection, action_id, status):
        return BookingResolution(self._view(await self._set_status(connection, action_id, status)))
