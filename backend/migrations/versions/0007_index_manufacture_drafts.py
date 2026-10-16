"""Index Manufacture: table of index drafts worked out with the assistant.

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-24
"""

from alembic import op
import sqlalchemy as sa

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "manufacture_drafts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(128), nullable=False, index=True),
        sa.Column("intent", sa.String(2000), nullable=False, server_default=""),
        sa.Column("description", sa.String(4000), nullable=False, server_default=""),
        sa.Column("methodology", sa.String(4000), nullable=False, server_default=""),
        sa.Column("keywords", sa.JSON(), nullable=False),
        sa.Column("tags", sa.JSON(), nullable=False),
        sa.Column("min_liquidity", sa.Float(), nullable=False, server_default="10000"),
        sa.Column("markets", sa.JSON(), nullable=False),
        sa.Column("messages", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="draft", index=True),
        sa.Column("published_index", sa.String(128)),
        sa.Column("model", sa.String(64)),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("manufacture_drafts")
