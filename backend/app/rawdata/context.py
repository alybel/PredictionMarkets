"""What a raw job gets (``Context``) and returns (``Fetched``)."""

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker

from backend.app.rawdata.models import RawFetchJob
from backend.app.rawdata.streams import GetRaw

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Fetched:
    summary: dict
    files: list[str]  # staging files (relative paths) of every answer this job saw, also unchanged ones


@dataclass
class Context:
    factory: sessionmaker
    job_id: int
    job: str
    trigger: str
    get_raw: GetRaw
    staging: Path | None
    now: Callable[[], datetime]
    options: dict = field(default_factory=dict)
    kind: str = ""  # fetcher kind; ``job`` may carry a scope ("history:<hash>")

    def progress(self, done: int, total: int | None, label: str) -> None:
        """Store the progress (also the heartbeat that keeps the job from being taken as stale); never fails the job."""
        try:
            with self.factory.begin() as session:
                row = session.get(RawFetchJob, self.job_id)
                row.progress, row.heartbeat_at = {"done": done, "total": total, "label": label}, self.now()
        except SQLAlchemyError as exc:
            logger.warning("raw job #%d: progress not stored: %s", self.job_id, exc)
