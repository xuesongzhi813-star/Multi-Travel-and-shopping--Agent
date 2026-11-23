"""Store demo hotel approvals, bookings and receipts."""

from alembic import op
from sqlalchemy import (
    BigInteger, CHAR, CheckConstraint, Column, Date, DateTime, ForeignKey, ForeignKeyConstraint,
    Index, Integer, MetaData, String, Table, UniqueConstraint, Uuid, func,
)
from sqlalchemy.dialects.postgresql import JSONB


revision = "0003_hotel_booking"
down_revision = "0002_tool_audit"
branch_labels = None
depends_on = None


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


def upgrade():
    schema = op.get_context().config.attributes["business_schema"]
    metadata = MetaData()
    for name in ("users", "threads", "messages", "hotels"):
        key = "id"
        Table(name, metadata, Column(key, String(32) if name == "hotels" else Uuid, primary_key=True), schema=schema)
    for table in booking_tables(metadata, schema):
        table.create(op.get_bind())


def downgrade():
    schema = op.get_context().config.attributes["business_schema"]
    for name in ("hotel_booking_receipts", "hotel_bookings", "hotel_booking_proposals"):
        op.drop_table(name, schema=schema)
