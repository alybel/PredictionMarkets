"""Risk topics and per-topic market assessments (risk index part 2); no existing table changes.

Revision ID: 0018
Revises: 0017
Create Date: 2026-10-17
"""

from alembic import op
import sqlalchemy as sa

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "risk_topics",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("slug", sa.String(64), nullable=False, unique=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("question", sa.String(512), nullable=False),
        sa.Column("search_terms", sa.JSON(), nullable=False),
        sa.Column("tags", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "risk_assessments",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("topic_id", sa.Integer(), sa.ForeignKey("risk_topics.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("market_id", sa.Integer(), sa.ForeignKey("market_directory.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("relevant", sa.Boolean(), nullable=False),
        sa.Column("rationale", sa.String(512), nullable=False),
        sa.Column("proposed_reading", sa.String(16), nullable=False),
        sa.Column("reading", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, index=True),
        sa.Column("model", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("topic_id", "market_id", name="uq_risk_assessment"),
    )


def downgrade() -> None:
    op.drop_table("risk_assessments")
    op.drop_table("risk_topics")
