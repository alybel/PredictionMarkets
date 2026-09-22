"""Add markets.tags and raise index rules to the global liquidity floor.

Tags hold the source categories/labels of a market (several per market).
Existing rules below 10 000 USD are lifted to the floor, matching the
engine, which never selects markets below it.

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-19
"""

from alembic import op
import sqlalchemy as sa

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

MIN_LIQUIDITY_USD = 10_000.0


def upgrade() -> None:
    op.add_column("markets", sa.Column("tags", sa.JSON(), nullable=False, server_default="[]"))
    op.execute(
        sa.text("UPDATE index_rules SET min_liquidity = :floor WHERE min_liquidity < :floor").bindparams(
            floor=MIN_LIQUIDITY_USD
        )
    )


def downgrade() -> None:
    op.drop_column("markets", "tags")
