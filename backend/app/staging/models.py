"""Raw layer of the staging files and the load log.

``raw_responses``: one row per staging file, body unchanged in ``content``. ``raw_load_log``: one entry per
staging file (loaded or failed). The uniqueness rules live in the database (migration 0010), so repeated and
parallel loads can never store a file twice.
"""

from datetime import datetime

from sqlalchemy import DateTime, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.db import Base
from backend.app.models import utcnow

LOADED = "loaded"
FAILED = "failed"


class RawResponse(Base):
    __tablename__ = "raw_responses"
    __table_args__ = (
        UniqueConstraint("staging_file", name="uq_raw_responses_staging_file"),
        UniqueConstraint("request_id", "fetched_at", "checksum", name="uq_raw_responses_request_time_checksum"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(32), index=True)
    dataset: Mapped[str] = mapped_column(String(64), index=True)
    url: Mapped[str] = mapped_column(Text)
    request_key: Mapped[str] = mapped_column(Text)
    request_id: Mapped[str] = mapped_column(String(64))  # sha256 of request_key
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    checksum: Mapped[str] = mapped_column(String(64))
    content: Mapped[str] = mapped_column(Text)
    staging_file: Mapped[str] = mapped_column(String(512))
    loaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class RawLoadLog(Base):
    __tablename__ = "raw_load_log"
    __table_args__ = (UniqueConstraint("staging_file", name="uq_raw_load_log_staging_file"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    staging_file: Mapped[str] = mapped_column(String(512))
    source: Mapped[str] = mapped_column(String(32))
    dataset: Mapped[str] = mapped_column(String(64))
    checksum: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), index=True)
    error: Mapped[str | None] = mapped_column(String(500))
    run_id: Mapped[str] = mapped_column(String(32))
    logged_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
