"""Add optional public provenance and synthetic flags to hotels."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision = "0004_hotel_catalog"
down_revision = "0003_hotel_booking"
branch_labels = None
depends_on = None


def upgrade():
    schema = op.get_context().config.attributes["business_schema"]
    columns = (
        sa.Column("source_url", sa.Text()), sa.Column("checked_on", sa.Date()),
        sa.Column("facilities", JSONB()), sa.Column("demo", sa.Boolean()), sa.Column("demo_terms", sa.Text()),
    )
    for column in columns:
        op.add_column("hotels", column, schema=schema)


def downgrade():
    schema = op.get_context().config.attributes["business_schema"]
    for name in ("demo_terms", "demo", "facilities", "checked_on", "source_url"):
        op.drop_column("hotels", name, schema=schema)
