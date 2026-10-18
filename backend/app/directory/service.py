"""Market directory: build from the raw data layer, "new markets" inbox, coverage per year and source, paged listing.

The directory never fetches by itself: refresh requests go to the raw data layer (``backend.app.rawdata.jobs``, job
"directory"), which stores the answers in ``raw_responses``; after every such job the directory is rebuilt from those raw
answers (``build``; subscribed below). Unchanged markets are never touched, so a repeated build adds and changes nothing.
New markets stay in the inbox until they are marked as reviewed.

Coverage counts, per source and year, the markets that existed in that year (from listing to end date, capped at
the current year); it is measured from the stored dates, not promised by the sources.
"""

import logging
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from backend.app.directory.build import SOURCES, build_directory
from backend.app.directory.groups import filtered
from backend.app.directory.models import STATUSES, DirectoryMarket, DirectoryRun
from backend.app.models import utcnow
from backend.app.quality.pit import as_utc
from backend.app.rawdata import jobs
from backend.app.rawdata.context import Context, Fetched
from backend.app.rawdata.models import DONE

logger = logging.getLogger(__name__)

PAGE_LIMIT = 500


def _iso(value: datetime | None) -> str | None:
    value = as_utc(value)
    return value.isoformat() if value else None


def _after_fetch(ctx: Context, fetched: Fetched) -> dict:
    """Subscriber of the raw job "directory": rebuild the directory from the raw answers the job saw."""
    with ctx.factory() as session:
        s = build_directory(session, ctx.trigger, fetched.files, {**fetched.summary, "job_id": ctx.job_id}, now=ctx.now, progress=ctx.progress)
    return {"run_id": s["run_id"], "sources": {k: {f: v[f] for f in ("found", "new", "marked_new", "changed", "broken")} for k, v in s["sources"].items()}}


jobs.subscribe("directory", "directory", _after_fetch)


def run_directory(session: Session, get_raw: Callable | None = None, staging: Path | None = None,
                  now: Callable[[], datetime] = utcnow, trigger: str = "manual", max_pages: int | None = None) -> dict:
    """Refresh request to the raw layer, waited for, then the rebuilt directory's run summary. While a raw job of the
    directory runs elsewhere, nothing is fetched twice: the directory is only built from the stored raw data."""
    session.commit()
    factory = sessionmaker(bind=session.get_bind(), expire_on_commit=False)
    job = jobs.request_refresh(factory, "directory", trigger, get_raw=get_raw, staging=staging, now=now, background=False, max_pages=max_pages)
    if job["duplicate"]:
        logger.info("directory run (%s): raw job #%d already running, building from the stored raw data only", trigger, job["id"])
        return {**build_directory(session, trigger, now=now), "fetch": {"duplicate": True, "job_id": job["id"]}}
    after = ((job["summary"] or {}).get("after") or {}).get("directory") or {}
    if job["status"] != DONE or "run_id" not in after:
        raise RuntimeError(f"Rohdaten-Abruf #{job['id']} fehlgeschlagen: {job['error'] or after.get('error')}")
    session.expire_all()
    return session.get(DirectoryRun, after["run_id"]).summary


def refresh_directory(session: Session, **kw) -> dict:
    """Daily job entry."""
    return run_directory(session, trigger="daily", **kw)


# ---- read side ------------------------------------------------------------------------------

def market_payload(r: DirectoryMarket) -> dict:
    return {"id": r.id, "source": r.source, "external_id": r.external_id, "title": r.title, "tags": list(r.tags or []),
            "listed_at": _iso(r.listed_at), "end_date": _iso(r.end_date), "status": r.status, "first_seen_at": _iso(r.first_seen_at),
            "is_new": r.is_new, "reviewed_at": _iso(r.reviewed_at), "group_key": r.group_key, "group_title": r.group_title}


def list_markets(session: Session, q: str = "", source: str = "", status: str = "", limit: int = 50, offset: int = 0,
                 group: str = "") -> dict:
    """Paged markets; ``group`` limits them to the members of one group market (key from ``groups.list_groups``)."""
    d = DirectoryMarket
    query = filtered(q, source, status, group)
    total = session.scalar(select(func.count()).select_from(query.subquery()))
    rows = session.scalars(query.order_by(d.listed_at.desc().nulls_last(), d.id.desc()).limit(limit).offset(offset))
    return {"total": total or 0, "limit": limit, "offset": offset, "markets": [market_payload(r) for r in rows]}


def inbox(session: Session, since: datetime | None = None) -> dict:
    """New, not yet reviewed markets (newest first); ``since`` limits them to markets first seen from then on."""
    d = DirectoryMarket
    query = select(d).where(d.is_new.is_(True), d.reviewed_at.is_(None))
    if since is not None:
        query = query.where(d.first_seen_at >= since)
    sub = query.subquery()
    total, oldest = session.execute(select(func.count(), func.min(sub.c.first_seen_at))).one()
    rows = session.scalars(query.order_by(d.first_seen_at.desc(), d.id.desc()).limit(PAGE_LIMIT))
    return {"since": _iso(since or oldest), "count": total or 0, "markets": [market_payload(r) for r in rows]}


def mark_reviewed(session: Session, ids: list[int] | None = None) -> int:
    """Remove markets from the inbox (all when ``ids`` is None); returns the number marked."""
    d = DirectoryMarket
    query = select(d).where(d.is_new.is_(True), d.reviewed_at.is_(None))
    if ids is not None:
        query = query.where(d.id.in_(ids))
    rows = list(session.scalars(query))
    now = utcnow()
    for r in rows:
        r.reviewed_at = now
    session.commit()
    return len(rows)


def coverage(session: Session, today: datetime | None = None) -> dict:
    """Per source: markets existing per year, status counts and markets without a listing date."""
    current_year = (today or utcnow()).year
    d = DirectoryMarket
    years: dict[str, dict[int, int]] = {s: {} for s in SOURCES}
    statuses = {s: dict.fromkeys(STATUSES, 0) for s in SOURCES}
    undated = dict.fromkeys(SOURCES, 0)
    for source, listed, end, first_seen, status in session.execute(select(d.source, d.listed_at, d.end_date, d.first_seen_at, d.status)):
        years.setdefault(source, {})
        statuses.setdefault(source, dict.fromkeys(STATUSES, 0))[status] = statuses[source].get(status, 0) + 1
        if listed is None:
            undated[source] = undated.get(source, 0) + 1
        start = (listed or end or first_seen).year
        stop = min(end.year if end else current_year, current_year)  # no end date: still existing
        for year in range(min(start, stop), max(start, stop) + 1):
            years[source][year] = years[source].get(year, 0) + 1
    all_years = sorted({y for per in years.values() for y in per})
    return {"years": all_years, "sources": {s: {str(y): per.get(y, 0) for y in all_years} for s, per in years.items()},
            "totals": {s: sum(c.values()) for s, c in statuses.items()}, "statuses": statuses, "undated": undated}


def last_run(session: Session) -> dict | None:
    run = session.scalar(select(DirectoryRun).order_by(DirectoryRun.id.desc()).limit(1))
    if run is None:
        return None
    return {"id": run.id, "trigger": run.trigger, "started_at": _iso(run.started_at), "finished_at": _iso(run.finished_at), "summary": run.summary}
