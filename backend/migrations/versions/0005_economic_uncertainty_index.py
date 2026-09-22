"""Tables of the Economic Uncertainty Index: daily snapshots, classifications,
monthly compositions, index values and the event calendar.

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-22
"""

from alembic import op
import sqlalchemy as sa

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "eui_snapshot_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("snapshot_date", sa.Date(), nullable=False, index=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("source_counts", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("eligible_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("note", sa.String(512)),
    )
    op.create_table(
        "eui_market_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("run_id", sa.Integer(), sa.ForeignKey("eui_snapshot_runs.id"), nullable=False, index=True),
        sa.Column("snapshot_date", sa.Date(), nullable=False, index=True),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("external_id", sa.String(128), nullable=False),
        sa.Column("title", sa.String(512), nullable=False),
        sa.Column("tags", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("url", sa.String(512)),
        sa.Column("price", sa.Float()),
        sa.Column("best_bid", sa.Float()),
        sa.Column("best_ask", sa.Float()),
        sa.Column("spread", sa.Float()),
        sa.Column("depth_usd", sa.Float(), nullable=False, server_default="0"),
        sa.Column("volume_24h_usd", sa.Float(), nullable=False, server_default="0"),
        sa.Column("liquidity_score", sa.Float(), nullable=False, server_default="0"),
        sa.Column("end_date", sa.DateTime(timezone=True)),
        sa.Column("active", sa.Boolean()),
        sa.Column("eligible", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("exclusion_reason", sa.String(64)),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("snapshot_date", "source", "external_id", name="uq_eui_snapshot_market"),
    )
    op.create_table(
        "eui_classifications",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("external_id", sa.String(128), nullable=False),
        sa.Column("title", sa.String(512), nullable=False),
        sa.Column("is_economic", sa.Boolean(), nullable=False, index=True),
        sa.Column("polarity", sa.String(16), nullable=False, server_default="symmetric"),
        sa.Column("rationale", sa.String(512)),
        sa.Column("model", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="proposed", index=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reviewed_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("source", "external_id", name="uq_eui_classification_market"),
    )
    op.create_table(
        "eui_compositions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("period", sa.String(7), nullable=False, unique=True, index=True),
        sa.Column("snapshot_date", sa.Date(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("constituents", sa.JSON(), nullable=False, server_default="[]"),
    )
    op.create_table(
        "eui_index_values",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("value_date", sa.Date(), nullable=False, unique=True, index=True),
        sa.Column("value", sa.Float()),
        sa.Column("constituent_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("composition_id", sa.Integer(), sa.ForeignKey("eui_compositions.id")),
        sa.Column("details", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "eui_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("event_date", sa.Date(), nullable=False, index=True),
        sa.Column("label", sa.String(128), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.UniqueConstraint("event_date", "label", name="uq_eui_event"),
    )


def downgrade() -> None:
    for table in ("eui_events", "eui_index_values", "eui_compositions", "eui_classifications", "eui_market_snapshots", "eui_snapshot_runs"):
        op.drop_table(table)
