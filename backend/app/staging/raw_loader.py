"""Staging -> raw: load new staging files 1:1 into ``raw_responses`` and keep the load log.

Per file one transaction (raw row + log entry): all or nothing, so an abort never leaves half data and the
next run continues with the files not logged yet. Inserts skip existing rows silently (``ON CONFLICT DO
NOTHING`` on the database uniqueness rules), so repeated and parallel runs give the same data. A broken file
is logged as ``failed`` and skipped; a database error is not logged per file, the next run retries it.

CLI: ``python -m backend.app.staging.raw_loader``
"""

import logging
import time as clock
import uuid
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from backend.app.logging_setup import configure_logging
from backend.app.staging.files import StagedFile, StagingError, list_staged, read_staged, sha256, staging_root
from backend.app.staging.models import FAILED, LOADED, RawLoadLog, RawResponse

logger = logging.getLogger("backend.app.staging.raw_loader")  # stable name also under `python -m`

_INSERTS = {"postgresql": postgresql.insert, "sqlite": sqlite.insert}


def insert_ignore(session: Session, model, values: dict) -> int:
    """Insert one row, silently skipped on a uniqueness conflict; returns 1 when inserted, else 0."""
    insert = _INSERTS[session.get_bind().dialect.name]
    return session.execute(insert(model).values(**values).on_conflict_do_nothing()).rowcount


def raw_values(f: StagedFile) -> dict:
    return {"source": f.source, "dataset": f.dataset, "url": f.url, "request_key": f.request_key,
            "request_id": sha256(f.request_key.encode()), "fetched_at": f.fetched_at, "checksum": f.checksum,
            "content": f.content, "staging_file": f.rel_path}


def log_entry(session: Session, rel: str, status: str, run_id: str, checksum: str | None = None, error: str | None = None) -> int:
    source, dataset = (rel.split("/") + ["?", "?"])[:2]
    return insert_ignore(session, RawLoadLog, {"staging_file": rel, "source": source, "dataset": dataset, "checksum": checksum,
                                               "status": status, "error": error and error[:500], "run_id": run_id})


def load_staging(factory: sessionmaker, root: Path | None = None) -> dict:
    root = Path(root) if root else staging_root()
    run_id = uuid.uuid4().hex
    started = clock.monotonic()
    files = list_staged(root) if root.exists() else []
    with factory() as session:
        known = set(session.scalars(select(RawLoadLog.staging_file)))
    summary = {"run_id": run_id, "found": len(files), "new": 0, "skipped": 0, "failed": 0, "failures": []}
    logger.info("raw load %s started: %d staging files in %s, %d already logged", run_id, len(files), root, len(known))
    for body in files:
        rel = body.relative_to(root).as_posix()
        if rel in known:
            summary["skipped"] += 1
            continue
        try:
            staged = read_staged(root, body)
        except (StagingError, OSError) as exc:
            summary["failed"] += 1
            summary["failures"].append({"file": rel, "error": str(exc)})
            logger.warning("raw load %s: broken staging file %s skipped: %s", run_id, rel, exc)
            try:
                with factory.begin() as session:
                    log_entry(session, rel, FAILED, run_id, error=str(exc))
            except SQLAlchemyError as db_exc:
                logger.error("raw load %s: could not log failure of %s: %s", run_id, rel, db_exc)
            continue
        try:
            with factory.begin() as session:  # one file = one transaction
                added = insert_ignore(session, RawResponse, raw_values(staged))
                logged = log_entry(session, rel, LOADED, run_id, checksum=staged.checksum)
        except SQLAlchemyError as exc:
            summary["failed"] += 1
            summary["failures"].append({"file": rel, "error": str(exc)[:200]})
            logger.error("raw load %s: database error on %s, retried next run: %s", run_id, rel, exc)
            continue
        if added and logged:
            summary["new"] += 1
            logger.debug("raw load %s: %s loaded", run_id, rel)
        else:  # loaded meanwhile by a parallel run
            summary["skipped"] += 1
    summary["duration_s"] = round(clock.monotonic() - started, 2)
    logger.info("raw load %s finished: found %d, new %d, skipped %d, failed %d in %.2f s", run_id, summary["found"],
                summary["new"], summary["skipped"], summary["failed"], summary["duration_s"])
    return summary


def main() -> None:
    from backend.app.db import make_engine, make_session_factory

    configure_logging()
    engine = make_engine()
    try:
        factory = make_session_factory(engine)
        summary = load_staging(factory)
        from backend.app.metrics.service import refresh_safely
        with factory() as session:
            refresh_safely(session, "staging load (CLI)")
    finally:
        engine.dispose()
    print({k: v for k, v in summary.items() if k != "failures"})


if __name__ == "__main__":
    main()
