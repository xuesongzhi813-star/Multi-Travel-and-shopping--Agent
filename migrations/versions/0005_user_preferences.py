"""Store explicit user-scoped hotel preferences."""

from alembic import op
import sqlalchemy as sa


revision = "0005_user_preferences"
down_revision = "0004_hotel_catalog"
branch_labels = None
depends_on = None


def upgrade() -> None:
    schema = op.get_context().config.attributes["business_schema"]
    op.create_table(
        "user_preferences",
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey(f"{schema}.users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("room_type", sa.String(32)),
        sa.Column("area", sa.String(32)),
        sa.Column("max_total_chf_minor", sa.Integer()),
        sa.CheckConstraint("room_type = 'standard'", name="user_preferences_room_type"),
        sa.CheckConstraint("area IN ('airport', 'city')", name="user_preferences_area"),
        sa.CheckConstraint("max_total_chf_minor BETWEEN 1 AND 1000000", name="user_preferences_budget"),
        schema=schema,
    )


def downgrade() -> None:
    schema = op.get_context().config.attributes["business_schema"]
    op.drop_table("user_preferences", schema=schema)
