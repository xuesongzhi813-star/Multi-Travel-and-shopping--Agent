"""Add an audit table without business foreign keys."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision = "0002_tool_audit"
down_revision = "0001_business"
branch_labels = None
depends_on = None


def upgrade():
    schema = op.get_context().config.attributes["business_schema"]
    op.create_table(
        "tool_audit_logs",
        sa.Column("run_id", sa.Uuid, primary_key=True),
        sa.Column("tool_call_id", sa.String(160), primary_key=True),
        sa.Column("thread_id", sa.Uuid),
        sa.Column("action_id", sa.Uuid),
        sa.Column("tool_name", sa.String(80), nullable=False),
        sa.Column("source", sa.String(80), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("error_kind", sa.String(40)),
        sa.Column("arguments", JSONB, nullable=False),
        sa.Column("result_summary", JSONB, nullable=False),
        sa.Column("retry_count", sa.Integer, nullable=False),
        sa.Column("elapsed_ms", sa.Integer, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        schema=schema,
    )


def downgrade():
    schema = op.get_context().config.attributes["business_schema"]
    op.drop_table("tool_audit_logs", schema=schema)
