"""Market directory (risk index part 1): every market of both sources and the load runs; no existing table changes.

Revision ID: 0017
Revises: 0016
Create Date: 2026-10-17
"""

from alembic import op
import sqlalchemy as sa

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "market_directory",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("source", sa.String(32), nullable=False, index=True),
        sa.Column("external_id", sa.String(128), nullable=False),
        sa.Column("title", sa.String(512), nullable=False),
        sa.Column("tags", sa.JSON(), nullable=False),
        sa.Column("listed_at", sa.DateTime(timezone=True)),
        sa.Column("end_date", sa.DateTime(timezone=True)),
        sa.Column("status", sa.String(16), nullable=False, index=True),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("first_seen_run", sa.Integer()),
        sa.Column("updated_at", sa.DateTime(timezone=True)),
        sa.Column("is_new", sa.Boolean(), nullable=False, index=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("source", "external_id", name="uq_market_directory_market"),
    )
    op.create_table(
        "market_directory_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("trigger", sa.String(16), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("summary", sa.JSON()),
    )


def downgrade() -> None:
    op.drop_table("market_directory_runs")
    op.drop_table("market_directory")
