"""Metric catalog: status, release, block and last check results per metric.

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-15
"""

from alembic import op
import sqlalchemy as sa

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "metric_states",
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="draft"),
        sa.Column("status_reason", sa.String(500)),
        sa.Column("validated_at", sa.DateTime(timezone=True)),
        sa.Column("blocked_at", sa.DateTime(timezone=True)),
        sa.Column("checked_at", sa.DateTime(timezone=True)),
        sa.Column("checks", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("metric_states")
