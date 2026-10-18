"""Market groups in the directory (1018-2043): group key and title of the source event per market.

Existing rows keep NULL (a group of one) until the next build re-reads every raw answer (build version 2).

Revision ID: 0022
Revises: 0021
Create Date: 2026-10-18
"""

from alembic import op
import sqlalchemy as sa

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("market_directory") as batch:
        batch.add_column(sa.Column("group_key", sa.String(128)))
        batch.add_column(sa.Column("group_title", sa.String(512)))
    op.create_index("ix_market_directory_group", "market_directory", ["source", "group_key"])


def downgrade() -> None:
    op.drop_index("ix_market_directory_group", table_name="market_directory")
    with op.batch_alter_table("market_directory") as batch:
        batch.drop_column("group_title")
        batch.drop_column("group_key")
