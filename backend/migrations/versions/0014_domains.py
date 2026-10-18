"""Domains with full raw history (tables ``domains`` and ``domain_markets``).

Only new tables; snapshots, events and indices stay unchanged.

Revision ID: 0014
Revises: 0013
Create Date: 2026-10-16
"""

from alembic import op
import sqlalchemy as sa

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "domains",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(120), nullable=False, unique=True),
        sa.Column("topic", sa.String(200), nullable=False),
        sa.Column("subtopic", sa.String(200)),
        sa.Column("terms", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_run_at", sa.DateTime(timezone=True)),
        sa.Column("last_run", sa.JSON()),
    )
    op.create_table(
        "domain_markets",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("domain_id", sa.Integer(), sa.ForeignKey("domains.id", ondelete="CASCADE"), nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("external_id", sa.String(128), nullable=False),
        sa.Column("title", sa.String(512), nullable=False),
        sa.Column("url", sa.String(512)),
        sa.Column("tags", sa.JSON(), nullable=False),
        sa.Column("end_date", sa.DateTime(timezone=True)),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("found_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("loaded_at", sa.DateTime(timezone=True)),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.UniqueConstraint("domain_id", "source", "external_id", name="uq_domain_markets_market"),
    )
    op.create_index("ix_domain_markets_domain_id", "domain_markets", ["domain_id"])


def downgrade() -> None:
    op.drop_index("ix_domain_markets_domain_id", table_name="domain_markets")
    op.drop_table("domain_markets")
    op.drop_table("domains")
