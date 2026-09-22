"""Initial schema: markets and index_rules.

Revision ID: 0001
Revises:
Create Date: 2026-09-17
"""

from alembic import op
import sqlalchemy as sa

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "markets",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("external_id", sa.String(length=128), nullable=False),
        sa.Column("title", sa.String(length=512), nullable=False),
        sa.Column("category", sa.String(length=128), nullable=True),
        sa.Column("price", sa.Float(), nullable=True),
        sa.Column("volume", sa.Float(), nullable=True),
        sa.Column("liquidity", sa.Float(), nullable=True),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("source", "external_id", name="uq_markets_source_external_id"),
    )
    op.create_index("ix_markets_source", "markets", ["source"])
    op.create_index("ix_markets_category", "markets", ["category"])

    op.create_table(
        "index_rules",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("index_name", sa.String(length=128), nullable=False),
        sa.Column("categories", sa.JSON(), nullable=False),
        sa.Column("keywords", sa.JSON(), nullable=False),
        sa.Column("min_liquidity", sa.Float(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_index_rules_index_name", "index_rules", ["index_name"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_index_rules_index_name", table_name="index_rules")
    op.drop_table("index_rules")
    op.drop_index("ix_markets_category", table_name="markets")
    op.drop_index("ix_markets_source", table_name="markets")
    op.drop_table("markets")
