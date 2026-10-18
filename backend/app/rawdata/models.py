"""``raw_fetch_jobs``: one row per refresh request of the raw data layer (who asked, progress, result).

``active`` is True while a job runs and NULL afterwards; the unique rule on (job, active) lets the database itself refuse a
second running job of the same kind (NULL values never collide), also across processes.
"""

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.db import Base
from backend.app.models import utcnow

RUNNING, DONE, FAILED = "running", "done", "failed"


class RawFetchJob(Base):
    __tablename__ = "raw_fetch_jobs"
    __table_args__ = (UniqueConstraint("job", "active", name="uq_raw_fetch_jobs_active"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    job: Mapped[str] = mapped_column(String(32), index=True)
    trigger: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16), index=True)
    active: Mapped[bool | None] = mapped_column(Boolean)
    progress: Mapped[dict | None] = mapped_column(JSON)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    heartbeat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    summary: Mapped[dict | None] = mapped_column(JSON)
    error: Mapped[str | None] = mapped_column(String(500))
