"""Index builder from validated metrics: metric indices and market-basket metrics from Index Manufacture.

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-15
"""

from alembic import op
import sqlalchemy as sa

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "custom_metrics",
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("calculation", sa.String(4000), nullable=False, server_default=""),
        sa.Column("markets", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("draft_id", sa.Integer()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "metric_indices",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("components", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_metric_indices_name", "metric_indices", ["name"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_metric_indices_name", table_name="metric_indices")
    op.drop_table("metric_indices")
    op.drop_table("custom_metrics")
