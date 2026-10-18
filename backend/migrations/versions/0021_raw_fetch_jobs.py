"""Raw fetch jobs: refresh requests of the raw data layer (1017-1426).

Carries the resume positions of the last market directory run over as a finished "directory" job, so the raw layer
continues the full listings where the directory stopped.

Revision ID: 0021
Revises: 0020
Create Date: 2026-10-17
"""

import json
from datetime import datetime

from alembic import op
import sqlalchemy as sa

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    jobs = op.create_table(
        "raw_fetch_jobs",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("job", sa.String(32), nullable=False),
        sa.Column("trigger", sa.String(32), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("active", sa.Boolean, nullable=True),
        sa.Column("progress", sa.JSON, nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("summary", sa.JSON, nullable=True),
        sa.Column("error", sa.String(500), nullable=True),
        sa.UniqueConstraint("job", "active", name="uq_raw_fetch_jobs_active"),
    )
    op.create_index("ix_raw_fetch_jobs_job", "raw_fetch_jobs", ["job"])
    op.create_index("ix_raw_fetch_jobs_status", "raw_fetch_jobs", ["status"])
    rows = op.get_bind().execute(sa.text("SELECT started_at, summary FROM market_directory_runs WHERE finished_at IS NOT NULL ORDER BY id DESC")).all()
    for started_at, summary in rows:
        summary = json.loads(summary) if isinstance(summary, str) else summary
        started_at = datetime.fromisoformat(started_at) if isinstance(started_at, str) else started_at  # SQLite returns text
        if summary and "positions" in summary:
            op.bulk_insert(jobs, [{"job": "directory", "trigger": "migration", "status": "done", "started_at": started_at,
                                   "heartbeat_at": started_at, "finished_at": started_at, "summary": {"positions": summary["positions"]}}])
            break


def downgrade() -> None:
    op.drop_index("ix_raw_fetch_jobs_status", table_name="raw_fetch_jobs")
    op.drop_index("ix_raw_fetch_jobs_job", table_name="raw_fetch_jobs")
    op.drop_table("raw_fetch_jobs")
