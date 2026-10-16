"""Stock correlation: daily closes of the top-100 US stocks.

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-15
"""

from alembic import op
import sqlalchemy as sa

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "stock_prices",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("ticker", sa.String(16), nullable=False, index=True),
        sa.Column("price_date", sa.Date(), nullable=False, index=True),
        sa.Column("close", sa.Float(), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("ticker", "price_date", name="uq_stock_prices_ticker_date"),
    )


def downgrade() -> None:
    op.drop_table("stock_prices")
