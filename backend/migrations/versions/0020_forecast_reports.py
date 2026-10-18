"""Forecast report per metric index (risk index 6/7).

Revision ID: 0020
Revises: 0019
Create Date: 2026-10-17
"""

from alembic import op
import sqlalchemy as sa

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "forecast_reports",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("index_name", sa.String(128), nullable=False),
        sa.Column("as_of", sa.Date, nullable=False),
        sa.Column("trigger", sa.String(16), nullable=False),
        sa.Column("payload", sa.JSON, nullable=False),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_forecast_reports_index_name", "forecast_reports", ["index_name"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_forecast_reports_index_name", table_name="forecast_reports")
    op.drop_table("forecast_reports")
