"""Events: bundle outcome markets of the same source event (tables ``events`` and ``event_markets``).

Only new tables; markets, snapshots and indices stay unchanged. Events fill on the next snapshot
or ``POST /events/sync``.

Revision ID: 0013
Revises: 0012
Create Date: 2026-10-16
"""

from alembic import op
import sqlalchemy as sa

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("event_key", sa.String(128), nullable=False),
        sa.Column("title", sa.String(512), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("source", "event_key", name="uq_events_source_event_key"),
    )
    op.create_index("ix_events_source", "events", ["source"])
    op.create_table(
        "event_markets",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("event_id", sa.Integer(), sa.ForeignKey("events.id", ondelete="CASCADE"), nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("external_id", sa.String(128), nullable=False),
        sa.Column("title", sa.String(512), nullable=False),
        sa.Column("outcome_label", sa.String(256)),
        sa.Column("price", sa.Float()),
        sa.Column("volume_24h_usd", sa.Float()),
        sa.Column("url", sa.String(512)),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("source", "external_id", name="uq_event_markets_source_external_id"),
    )
    op.create_index("ix_event_markets_event_id", "event_markets", ["event_id"])


def downgrade() -> None:
    op.drop_index("ix_event_markets_event_id", table_name="event_markets")
    op.drop_table("event_markets")
    op.drop_index("ix_events_source", table_name="events")
    op.drop_table("events")
