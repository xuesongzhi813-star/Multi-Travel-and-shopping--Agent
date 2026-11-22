"""Create the P2 business schema."""

from alembic import op
import sqlalchemy as sa

revision = "0001_business"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    schema = op.get_context().config.attributes["business_schema"]

    def created_at():
        return sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now())

    op.create_table(
        "users", sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column("username", sa.String(80), nullable=False, unique=True),
        sa.Column("password_hash", sa.Text, nullable=False), schema=schema,
    )
    op.create_table(
        "auth_sessions", sa.Column("token_digest", sa.CHAR(64), primary_key=True),
        sa.Column("user_id", sa.Uuid, sa.ForeignKey(f"{schema}.users.id"), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False), schema=schema,
    )
    op.create_table(
        "threads", sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column("user_id", sa.Uuid, sa.ForeignKey(f"{schema}.users.id"), nullable=False),
        created_at(), schema=schema,
    )
    op.create_table(
        "messages", sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column("thread_id", sa.Uuid, sa.ForeignKey(f"{schema}.threads.id"), nullable=False),
        sa.Column("content", sa.Text, nullable=False), created_at(), schema=schema,
    )
    op.create_table(
        "flight_itineraries", sa.Column("reference", sa.String(32), primary_key=True),
        sa.Column("user_id", sa.Uuid, sa.ForeignKey(f"{schema}.users.id"), nullable=False),
        sa.Column("flight_number", sa.String(16), nullable=False),
        sa.Column("origin", sa.CHAR(3), nullable=False),
        sa.Column("destination", sa.CHAR(3), nullable=False),
        sa.Column("departure_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("arrival_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("arrival_timezone", sa.String(64), nullable=False),
        sa.CheckConstraint("arrival_at > departure_at", name="itinerary_chronology"), schema=schema,
    )
    op.create_table(
        "hotels", sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("area", sa.String(32), nullable=False), schema=schema,
    )
    op.create_table(
        "hotel_inventory",
        sa.Column("hotel_id", sa.String(32), sa.ForeignKey(f"{schema}.hotels.id"), primary_key=True),
        sa.Column("stay_date", sa.Date, primary_key=True),
        sa.Column("room_type", sa.String(32), primary_key=True),
        sa.Column("quantity", sa.Integer, nullable=False),
        sa.Column("price_minor", sa.Integer, nullable=False),
        sa.Column("currency", sa.CHAR(3), nullable=False),
        sa.CheckConstraint("quantity >= 0", name="inventory_quantity_nonnegative"),
        sa.CheckConstraint("price_minor >= 0", name="inventory_price_nonnegative"), schema=schema,
    )


def downgrade():
    schema = op.get_context().config.attributes["business_schema"]
    for table in ("hotel_inventory", "hotels", "flight_itineraries", "messages", "threads", "auth_sessions", "users"):
        op.drop_table(table, schema=schema)
