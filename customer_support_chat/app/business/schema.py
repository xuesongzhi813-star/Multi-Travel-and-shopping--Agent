"""SQLAlchemy Core tables used by the business repositories."""

from dataclasses import dataclass

from sqlalchemy import (
    BigInteger, Boolean, CHAR, CheckConstraint, Column, Date, DateTime, ForeignKey, ForeignKeyConstraint, Index, Integer, MetaData,
    String, Table, Text, UniqueConstraint, Uuid, func,
)

from sqlalchemy.dialects.postgresql import JSONB

from .database import validate_schema


@dataclass(frozen=True)
class BusinessTables:
    users: Table
    auth_sessions: Table
    threads: Table
    messages: Table
    flight_itineraries: Table
    hotels: Table
    hotel_inventory: Table
    tool_audit_logs: Table
    hotel_booking_proposals: Table
    hotel_bookings: Table
    hotel_booking_receipts: Table


def tables(schema: str) -> BusinessTables:
    validate_schema(schema)
    metadata = MetaData(schema=schema)

    def created_at():
        return Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now())

    users = Table(
        "users", metadata,
        Column("id", Uuid, primary_key=True),
        Column("username", String(80), nullable=False, unique=True),
        Column("password_hash", Text, nullable=False),
    )
    auth_sessions = Table(
        "auth_sessions", metadata,
        Column("token_digest", CHAR(64), primary_key=True),
        Column("user_id", Uuid, ForeignKey(f"{schema}.users.id"), nullable=False),
        Column("expires_at", DateTime(timezone=True), nullable=False),
    )
    threads = Table(
        "threads", metadata,
        Column("id", Uuid, primary_key=True),
        Column("user_id", Uuid, ForeignKey(f"{schema}.users.id"), nullable=False),
        created_at(),
    )
    messages = Table(
        "messages", metadata,
        Column("id", Uuid, primary_key=True),
        Column("thread_id", Uuid, ForeignKey(f"{schema}.threads.id"), nullable=False),
        Column("content", Text, nullable=False),
        created_at(),
    )
    itineraries = Table(
        "flight_itineraries", metadata,
        Column("reference", String(32), primary_key=True),
        Column("user_id", Uuid, ForeignKey(f"{schema}.users.id"), nullable=False),
        Column("flight_number", String(16), nullable=False),
        Column("origin", CHAR(3), nullable=False),
        Column("destination", CHAR(3), nullable=False),
        Column("departure_at", DateTime(timezone=True), nullable=False),
        Column("arrival_at", DateTime(timezone=True), nullable=False),
        Column("arrival_timezone", String(64), nullable=False),
        CheckConstraint("arrival_at > departure_at", name="itinerary_chronology"),
    )
    hotels = Table(
        "hotels", metadata,
        Column("id", String(32), primary_key=True),
        Column("name", String(120), nullable=False),
        Column("area", String(32), nullable=False),
        Column("source_url", Text), Column("checked_on", Date), Column("facilities", JSONB),
        Column("demo", Boolean), Column("demo_terms", Text),
    )
    inventory = Table(
        "hotel_inventory", metadata,
        Column("hotel_id", String(32), ForeignKey(f"{schema}.hotels.id"), primary_key=True),
        Column("stay_date", Date, primary_key=True),
        Column("room_type", String(32), primary_key=True),
        Column("quantity", Integer, nullable=False),
        Column("price_minor", Integer, nullable=False),
        Column("currency", CHAR(3), nullable=False),
        CheckConstraint("quantity >= 0", name="inventory_quantity_nonnegative"),
        CheckConstraint("price_minor >= 0", name="inventory_price_nonnegative"),
    )
    audit = Table(
        "tool_audit_logs", metadata,
        Column("run_id", Uuid, primary_key=True),
        Column("tool_call_id", String(160), primary_key=True),
        Column("thread_id", Uuid),
        Column("action_id", Uuid),
        Column("tool_name", String(80), nullable=False),
        Column("source", String(80), nullable=False),
        Column("status", String(32), nullable=False),
        Column("error_kind", String(40)),
        Column("arguments", JSONB, nullable=False),
        Column("result_summary", JSONB, nullable=False),
        Column("retry_count", Integer, nullable=False),
        Column("elapsed_ms", Integer, nullable=False),
        Column("created_at", DateTime(timezone=True), nullable=False),
    )
    return BusinessTables(users, auth_sessions, threads, messages, itineraries, hotels, inventory, audit,
                          *booking_tables(metadata, schema))


def booking_tables(metadata, schema):
    proposals = Table(
        "hotel_booking_proposals", metadata,
        Column("action_id", Uuid, primary_key=True),
        Column("user_id", Uuid, ForeignKey(f"{schema}.users.id"), nullable=False),
        Column("thread_id", Uuid, ForeignKey(f"{schema}.threads.id"), nullable=False),
        Column("source_message_id", Uuid, ForeignKey(f"{schema}.messages.id"), nullable=False),
        Column("option_index", Integer, nullable=False),
        Column("hotel_id", String(32), ForeignKey(f"{schema}.hotels.id"), nullable=False),
        Column("hotel_name", String(120), nullable=False),
        Column("room_type", String(32), nullable=False),
        Column("check_in", Date, nullable=False),
        Column("check_out", Date, nullable=False),
        Column("total_minor", BigInteger, nullable=False),
        Column("currency", CHAR(3), nullable=False),
        Column("night_snapshot", JSONB, nullable=False),
        Column("status", String(24), nullable=False),
        Column("decision", String(7)),
        Column("created_at", DateTime(timezone=True), nullable=False),
        Column("expires_at", DateTime(timezone=True), nullable=False),
        UniqueConstraint("thread_id", "source_message_id", name="hotel_proposal_source_uq"),
        CheckConstraint("option_index BETWEEN 0 AND 4 AND total_minor >= 0", name="hotel_proposal_amount_ck"),
        CheckConstraint("check_out - check_in BETWEEN 1 AND 14", name="hotel_proposal_dates_ck"),
        CheckConstraint("expires_at > created_at", name="hotel_proposal_expiry_ck"),
        CheckConstraint(
            "(status = 'awaiting_decision' AND decision IS NULL) OR "
            "(status = 'decided' AND decision IS NOT NULL AND decision IN ('approve', 'reject')) OR "
            "(status IN ('booked', 'stale') AND decision IS NOT NULL AND decision = 'approve') OR "
            "(status = 'rejected' AND decision IS NOT NULL AND decision = 'reject') OR "
            "(status = 'expired' AND (decision IS NULL OR decision IN ('approve', 'reject')))",
            name="hotel_proposal_state_ck",
        ),
        schema=schema,
    )
    Index("hotel_proposal_pending_thread_uq", proposals.c.thread_id, unique=True,
          postgresql_where=proposals.c.status.in_(("awaiting_decision", "decided")))
    bookings = Table(
        "hotel_bookings", metadata,
        Column("booking_id", Uuid, primary_key=True),
        Column("action_id", Uuid, ForeignKey(f"{schema}.hotel_booking_proposals.action_id"), nullable=False, unique=True),
        Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
        UniqueConstraint("action_id", "booking_id", name="hotel_booking_pair_uq"),
        schema=schema,
    )
    receipts = Table(
        "hotel_booking_receipts", metadata,
        Column("action_id", Uuid, primary_key=True),
        Column("booking_id", Uuid, nullable=False, unique=True),
        Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
        ForeignKeyConstraint(("action_id", "booking_id"), (bookings.c.action_id, bookings.c.booking_id)),
        schema=schema,
    )
    return proposals, bookings, receipts
