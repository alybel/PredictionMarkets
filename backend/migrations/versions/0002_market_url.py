"""Add markets.url: web link to the market on its source platform.

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-18
"""

from alembic import op
import sqlalchemy as sa

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("markets", sa.Column("url", sa.String(length=512), nullable=True))


def downgrade() -> None:
    op.drop_column("markets", "url")
