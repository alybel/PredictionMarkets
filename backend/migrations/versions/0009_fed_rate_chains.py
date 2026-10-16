"""Fed rate chains: append-only raw layer of the FOMC decision markets.

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-15
"""

from alembic import op
import sqlalchemy as sa

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "fed_markets",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("source", sa.String(32), nullable=False, index=True),
        sa.Column("external_id", sa.String(128), nullable=False),
        sa.Column("event_id", sa.String(128), nullable=False),
        sa.Column("session_date", sa.Date(), nullable=False, index=True),
        sa.Column("outcome_bp", sa.Integer(), nullable=False),
        sa.Column("outcome_label", sa.String(128), nullable=False),
        sa.Column("title", sa.String(512), nullable=False),
        sa.Column("url", sa.String(512)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("source", "external_id", name="uq_fed_markets_source_external_id"),
    )
    op.create_table(
        "fed_prices",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("market_id", sa.Integer(), sa.ForeignKey("fed_markets.id"), nullable=False, index=True),
        sa.Column("price_date", sa.Date(), nullable=False, index=True),
        sa.Column("price", sa.Float(), nullable=False),
        sa.Column("volume_usd", sa.Float()),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("origin", sa.String(16), nullable=False),
        sa.UniqueConstraint("market_id", "price_date", name="uq_fed_prices_market_date"),
    )


def downgrade() -> None:
    op.drop_table("fed_prices")
    op.drop_table("fed_markets")
