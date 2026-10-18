"""Weighting method per metric index (risk index 5/7); existing indices keep ``manual`` (unchanged values).

Revision ID: 0019
Revises: 0018
Create Date: 2026-10-17
"""

from alembic import op
import sqlalchemy as sa

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("metric_indices", sa.Column("weighting", sa.String(16), nullable=False, server_default="manual"))


def downgrade() -> None:
    op.drop_column("metric_indices", "weighting")
