"""Structured custom metrics from agent step 2: ``custom_metrics.kind`` (basket | chain | single) and ``spec`` (links, rules, notes).

Existing rows become ``basket`` and keep computing as before; no data is changed.

Revision ID: 0016
Revises: 0015
Create Date: 2026-10-16
"""

from alembic import op
import sqlalchemy as sa

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("custom_metrics") as batch:
        batch.add_column(sa.Column("kind", sa.String(16), nullable=False, server_default="basket"))
        batch.add_column(sa.Column("spec", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("custom_metrics") as batch:
        batch.drop_column("spec")
        batch.drop_column("kind")
