"""Add markets.end_date and the price corridor to index rules.

Existing rules are backfilled with the default corridor (0.03–0.97) via
the server default, so near-decided markets drop out everywhere.

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-18
"""

from alembic import op
import sqlalchemy as sa

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("markets", sa.Column("end_date", sa.DateTime(timezone=True), nullable=True))
    op.add_column("index_rules", sa.Column("min_price", sa.Float(), nullable=False, server_default="0.03"))
    op.add_column("index_rules", sa.Column("max_price", sa.Float(), nullable=False, server_default="0.97"))


def downgrade() -> None:
    op.drop_column("index_rules", "max_price")
    op.drop_column("index_rules", "min_price")
    op.drop_column("markets", "end_date")
