"""Staging -> raw layer: unchanged API answers and the load log, uniqueness enforced by the database.

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-15
"""

from alembic import op
import sqlalchemy as sa

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "raw_responses",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("source", sa.String(32), nullable=False, index=True),
        sa.Column("dataset", sa.String(64), nullable=False, index=True),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("request_key", sa.Text(), nullable=False),
        sa.Column("request_id", sa.String(64), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("checksum", sa.String(64), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("staging_file", sa.String(512), nullable=False),
        sa.Column("loaded_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("staging_file", name="uq_raw_responses_staging_file"),
        sa.UniqueConstraint("request_id", "fetched_at", "checksum", name="uq_raw_responses_request_time_checksum"),
    )
    op.create_table(
        "raw_load_log",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("staging_file", sa.String(512), nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("dataset", sa.String(64), nullable=False),
        sa.Column("checksum", sa.String(64)),
        sa.Column("status", sa.String(16), nullable=False, index=True),
        sa.Column("error", sa.String(500)),
        sa.Column("run_id", sa.String(32), nullable=False),
        sa.Column("logged_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("staging_file", name="uq_raw_load_log_staging_file"),
    )


def downgrade() -> None:
    op.drop_table("raw_load_log")
    op.drop_table("raw_responses")
