"""Haiku pre-screening: uncertainty note, direction and reference value per
classification, plus the table of current indicator values.

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-23
"""

from alembic import op
import sqlalchemy as sa

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("eui_classifications") as batch:
        batch.add_column(sa.Column("uncertainty_note", sa.String(512)))
        batch.add_column(sa.Column("direction", sa.String(16), nullable=False, server_default="n/a"))
        batch.add_column(sa.Column("reference_indicator", sa.String(64)))
        batch.add_column(sa.Column("reference_value", sa.Float()))
    op.create_table(
        "eui_reference_values",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("indicator", sa.String(64), nullable=False, unique=True, index=True),
        sa.Column("label", sa.String(128), nullable=False),
        sa.Column("value", sa.Float()),
        sa.Column("unit", sa.String(16), nullable=False, server_default="%"),
        sa.Column("as_of", sa.Date()),
        sa.Column("note", sa.String(256)),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("eui_reference_values")
    with op.batch_alter_table("eui_classifications") as batch:
        batch.drop_column("reference_value")
        batch.drop_column("reference_indicator")
        batch.drop_column("direction")
        batch.drop_column("uncertainty_note")
