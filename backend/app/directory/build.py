"""Build the market directory from ``raw_responses`` only (datasets ``directory_*`` of both sources) - no live request.

Input of one build: the raw answers a raw job just saw (``files``, also unchanged answers) plus every directory answer
stored after the last build (``raw_upto``, kept in the run summary). Answers are processed oldest first, so the latest
answer wins. ``Store`` touches a market only when an answer reports different data, so building again from the same
raw data changes nothing. An unusable answer (no JSON, no market list) is counted as ``broken``, logged and skipped.
A build of an older ``BUILD_VERSION`` (before market groups) makes the next build re-read every answer once.
"""

import logging
import time as clock
from collections.abc import Callable
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.directory.models import DirectoryRun
from backend.app.directory.parse import BrokenAnswer, answer_entries
from backend.app.directory.store import Store
from backend.app.models import utcnow
from backend.app.quality.pit import as_utc
from backend.app.staging.models import RawResponse

logger = logging.getLogger(__name__)

SOURCES = ("polymarket", "kalshi")
CHUNK = 50
BUILD_VERSION = 2  # 2: market groups stored
Progress = Callable[[int, int | None, str], None]


def _directory_rows():
    r = RawResponse
    return r.source.in_(SOURCES), r.dataset.like("directory_%")


def _mark(session: Session) -> int:
    """Highest raw id the directory was built from; 0 (re-read everything) after a build of an older version or without a mark."""
    for summary in session.scalars(select(DirectoryRun.summary).order_by(DirectoryRun.id.desc()).limit(20)):
        if summary and "raw_upto" in summary:
            return int(summary["raw_upto"]) if summary.get("build_version", 1) >= BUILD_VERSION else 0
    return 0


def _answer_ids(session: Session, mark: int, files: list[str]) -> list[int]:
    r = RawResponse
    found = dict(session.execute(select(r.id, r.fetched_at).where(*_directory_rows(), r.id > mark)).all())
    for i in range(0, len(files), 500):
        found.update(session.execute(select(r.id, r.fetched_at).where(*_directory_rows(), r.staging_file.in_(files[i:i + 500]))).all())
    return sorted(found, key=lambda i: (as_utc(found[i]), i))


def build_directory(session: Session, trigger: str = "manual", files: list[str] | None = None, fetch: dict | None = None,
                    now: Callable[[], datetime] = utcnow, progress: Progress | None = None, from_start: bool = False) -> dict:
    """One build run; ``fetch`` (summary of the raw job) is merged into the run summary. ``from_start`` re-reads every answer."""
    started = clock.monotonic()
    mark = 0 if from_start else _mark(session)
    run = DirectoryRun(trigger=trigger, started_at=now())
    session.add(run)
    session.commit()
    ids = _answer_ids(session, mark, files or [])
    logger.info("directory build %d started (%s): %d raw answers (after raw id %d, %d files of the raw job)", run.id, trigger, len(ids),
                mark, len(files or []))
    stores = {s: Store(session, s, run.id) for s in SOURCES}
    stats = {s: {"answers": 0, "found": 0, "malformed": 0, "broken": 0} for s in SOURCES}
    for i in range(0, len(ids), CHUNK):
        if progress:
            progress(i, len(ids), f"Verzeichnis aus Rohdaten: {i} von {len(ids)} Antworten")
        rows = {r.id: r for r in session.scalars(select(RawResponse).where(RawResponse.id.in_(ids[i:i + CHUNK])))}
        for raw_id in ids[i:i + CHUNK]:
            row = rows[raw_id]
            st = stats[row.source]
            st["answers"] += 1
            try:
                entries = list(answer_entries(row.source, row.dataset, row.content, now()))
                valid = [e for e in entries if e is not None]
                stores[row.source].add(valid, now())
            except BrokenAnswer as exc:
                st["broken"] += 1
                logger.warning("directory build %d: raw answer #%d (%s, %s) unusable, skipped: %s", run.id, raw_id, row.source, row.staging_file, exc)
                continue
            except Exception:  # one bad answer never stops the build
                session.rollback()
                st["broken"] += 1
                logger.exception("directory build %d: raw answer #%d (%s) failed", run.id, raw_id, row.staging_file)
                continue
            finally:
                session.expunge(row)
            st["found"] += len(valid)
            st["malformed"] += len(entries) - len(valid)
    sources = {}
    fetched = (fetch or {}).get("sources", {})
    for s in SOURCES:
        c, st = stores[s].counts, stats[s]
        f = fetched.get(s, {})
        sources[s] = {**st, **c, "skipped": c["unchanged"] + c["duplicates"] + st["malformed"], "errors": f.get("errors", 0),
                      "error_details": f.get("error_details", []), "requests": f.get("requests", 0), "staged": f.get("staged", 0)}
        logger.info("directory build %d %s: %d answers (%d broken), found %d, new %d (marked new %d), changed %d, unchanged %d, "
                    "duplicates %d, malformed %d", run.id, s, st["answers"], st["broken"], st["found"], c["new"], c["marked_new"], c["changed"],
                    c["unchanged"], c["duplicates"], st["malformed"])
    summary = {"run_id": run.id, "trigger": trigger, "sources": sources, "raw_upto": max([mark, *ids]), "build_version": BUILD_VERSION,
               **{k: fetch[k] for k in ("streams", "positions", "raw", "job_id") if fetch and k in fetch},
               "duration_s": round(clock.monotonic() - started, 1)}
    run.finished_at, run.summary = now(), summary
    session.commit()
    logger.info("directory build %d finished in %.1f s", run.id, summary["duration_s"])
    return summary
