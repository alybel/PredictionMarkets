"""Bookmarked catalog metrics from the agent mode "Metriken finden" (table ``metric_bookmarks``).

Only a new table; existing tables stay unchanged.

Revision ID: 0015
Revises: 0014
Create Date: 2026-10-16
"""

from alembic import op
import sqlalchemy as sa

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "metric_bookmarks",
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("metric_bookmarks")
