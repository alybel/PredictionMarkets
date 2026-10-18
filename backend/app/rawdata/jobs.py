"""Central entry of the raw data layer: refresh requests, background run with progress, no parallel run of the same job.

``request_refresh`` stores a ``raw_fetch_jobs`` row and runs the job (in a background thread by default) - or, while a job
of the same kind is still running, returns that one with ``duplicate=True`` and starts nothing. A running job without a
heartbeat for ``STALE_AFTER`` (e.g. the process died) is closed as failed, so it never blocks forever.

After the raw data are stored, the subscribers of the job run (e.g. the market directory builds itself from
``raw_responses``); the raw layer never imports them, they register via ``subscribe``. The business processes register
their request sequences as further kinds (``register``, see ``providers``); a ``scope`` (e.g. a hash of the requested
markets) makes the job name ``kind:scope``, so only equal requests wait for each other. The staging files a job saw
are kept in its summary (``files``, up to ``MAX_FILES``), so a process reads exactly that job's answers.
"""

import logging
import threading
import time as clock
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from backend.app.models import utcnow
from backend.app.quality.pit import as_utc
from backend.app.rawdata.context import Context, Fetched
from backend.app.rawdata.directory_fetch import fetch_directory
from backend.app.rawdata.models import DONE, FAILED, RUNNING, RawFetchJob
from backend.app.rawdata.streams import GetRaw, http_get_raw

logger = logging.getLogger(__name__)

FETCHERS: dict[str, Callable[[Context], Fetched]] = {"directory": fetch_directory}
STALE_AFTER = timedelta(minutes=15)
MAX_FILES = 5000
Subscriber = Callable[[Context, Fetched], dict | None]

_subscribers: dict[str, dict[str, Subscriber]] = {}
_keep_files: set[str] = set()
_lock = threading.Lock()
_threads: dict[int, threading.Thread] = {}


class UnknownJob(LookupError):
    pass


def register(kind: str, fetcher: Callable[[Context], Fetched]) -> None:
    """Add a process's request sequence as a job kind; its jobs keep their staging files in the summary."""
    FETCHERS[kind] = fetcher
    _keep_files.add(kind)


def subscribe(job: str, name: str, fn: Subscriber) -> None:
    """Run ``fn(ctx, fetched)`` after every successful fetch of ``job``; its result lands in ``summary["after"][name]``."""
    _subscribers.setdefault(job, {})[name] = fn


def _iso(value) -> str | None:
    value = as_utc(value)
    return value.isoformat() if value else None


def job_payload(row: RawFetchJob, duplicate: bool = False) -> dict:
    return {"id": row.id, "job": row.job, "trigger": row.trigger, "status": row.status, "running": bool(row.active),
            "progress": row.progress, "started_at": _iso(row.started_at), "finished_at": _iso(row.finished_at), "error": row.error,
            "summary": row.summary, "duplicate": duplicate}


def _active(session: Session, job: str, now) -> RawFetchJob | None:
    """The running job of this kind; a stale one is closed as failed first."""
    row = session.scalar(select(RawFetchJob).where(RawFetchJob.job == job, RawFetchJob.active.is_(True)))
    if row is not None and as_utc(row.heartbeat_at) < now - STALE_AFTER:
        logger.warning("raw job #%d %s: no heartbeat since %s, closed as failed", row.id, job, row.heartbeat_at)
        row.status, row.active, row.finished_at, row.error = FAILED, None, now, "abgebrochen: keine Rückmeldung mehr"
        session.commit()
        return None
    return row


def request_refresh(factory: sessionmaker, job: str, trigger: str, *, get_raw: GetRaw | None = None, staging: Path | None = None,
                    now=utcnow, background: bool = True, scope: str | None = None, **options) -> dict:
    """Accept a refresh request; returns the job (running in the background, or finished when ``background`` is False)."""
    if job not in FETCHERS:
        raise UnknownJob(f"unbekannter Rohdaten-Auftrag: {job!r}")
    kind, job = job, f"{job}:{scope}"[:32] if scope else job
    with _lock, factory() as session:
        moment = now()
        running = _active(session, job, moment)
        if running is None:
            row = RawFetchJob(job=job, trigger=trigger, status=RUNNING, active=True, started_at=moment, heartbeat_at=moment,
                              progress={"done": 0, "total": None, "label": "gestartet"})
            session.add(row)
            try:
                session.commit()
            except IntegrityError:  # another process started the same job just now
                session.rollback()
                running = _active(session, job, moment)
        if running is not None:
            logger.info("raw request %s (trigger %s): job #%d already running since %s, no second fetch", job, trigger, running.id,
                        running.started_at)
            return job_payload(running, duplicate=True)
        payload = job_payload(row)
    logger.info("raw request %s (trigger %s) accepted as job #%d (%s)", job, trigger, row.id, "background" if background else "inline")
    ctx = Context(factory, row.id, job, trigger, get_raw or http_get_raw, staging, now, options, kind)
    if not background:
        return _execute(ctx)
    thread = threading.Thread(target=_execute, args=(ctx,), name=f"raw-{job}-{row.id}", daemon=True)
    _threads[row.id] = thread
    thread.start()
    return payload


def _execute(ctx: Context) -> dict:
    started = clock.monotonic()
    logger.info("raw job #%d %s started (trigger %s)", ctx.job_id, ctx.job, ctx.trigger)
    status, error, summary = DONE, None, None
    try:
        fetched = FETCHERS[ctx.kind or ctx.job](ctx)
        summary, after = dict(fetched.summary), {}
        if ctx.kind in _keep_files and len(fetched.files) <= MAX_FILES:
            summary["files"] = fetched.files
        for name, fn in _subscribers.get(ctx.kind or ctx.job, {}).items():
            ctx.progress(1, 1, f"Aufbau aus Rohdaten: {name}")
            try:
                after[name] = fn(ctx, fetched)
            except Exception as exc:  # the raw data are stored; the consumer can build again later
                logger.exception("raw job #%d: subscriber %s failed", ctx.job_id, name)
                after[name] = {"error": str(exc)[:200]}
        summary["after"] = after
    except Exception as exc:
        logger.exception("raw job #%d %s failed (trigger %s)", ctx.job_id, ctx.job, ctx.trigger)
        status, error = FAILED, str(exc)[:500]
    duration = round(clock.monotonic() - started, 1)
    with ctx.factory() as session:
        row = session.get(RawFetchJob, ctx.job_id)
        row.status, row.active, row.finished_at, row.error = status, None, ctx.now(), error
        row.summary = {**(summary or {}), "duration_s": duration}
        row.progress = {"done": 1, "total": 1, "label": "fertig" if status == DONE else "fehlgeschlagen"}
        session.commit()
        payload = job_payload(row)
    errors = {s: v.get("errors") for s, v in (summary or {}).get("sources", {}).items()}
    logger.info("raw job #%d %s finished (trigger %s): %s in %.1f s, raw %s, errors %s, after %s", ctx.job_id, ctx.job, ctx.trigger, status,
                duration, (summary or {}).get("raw"), errors, sorted((summary or {}).get("after", {})))
    _threads.pop(ctx.job_id, None)
    return payload


def get_job(session: Session, job_id: int) -> dict | None:
    row = session.get(RawFetchJob, job_id)
    return job_payload(row) if row else None


def latest_job(session: Session, job: str) -> dict | None:
    row = session.scalar(select(RawFetchJob).where(RawFetchJob.job == job).order_by(RawFetchJob.id.desc()).limit(1))
    return job_payload(row) if row else None


def wait(job_id: int, timeout: float = 30) -> None:
    """Block until a background job ended (tests, CLI)."""
    thread = _threads.get(job_id)
    if thread is not None:
        thread.join(timeout)
