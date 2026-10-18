"""Raw job "directory": page through all market listings of both sources into staging, then staging -> ``raw_responses``.

Resume positions of the resumable streams and the start of the "since" streams come from the previous finished jobs,
so each run continues where the last one stopped. Consumers (the market directory) build from ``raw_responses`` only.
"""

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from backend.app.quality.pit import as_utc
from backend.app.rawdata.context import Context, Fetched
from backend.app.rawdata.models import DONE, RawFetchJob
from backend.app.rawdata.streams import HEAD_PAGES, MAX_PAGES, STREAMS, Crawler
from backend.app.staging.files import Stager
from backend.app.staging.raw_loader import load_staging

logger = logging.getLogger(__name__)

JOB = "directory"


def _previous(ctx: Context) -> list[RawFetchJob]:
    j = RawFetchJob
    with ctx.factory() as session:
        return list(session.scalars(select(j).where(j.job == JOB, j.status == DONE, j.id != ctx.job_id).order_by(j.id.desc()).limit(20)))


def _since(previous: list[RawFetchJob], now: datetime) -> int:
    """Unix time for the "since" streams: day start before the last finished job (first job: yesterday).
    Whole days keep repeated jobs of one day identical requests, so the staging folder stores them only once."""
    last = as_utc(previous[0].started_at) if previous else None
    day = (min(last or now, now) - timedelta(days=1)).date()
    return int(datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp())


def _positions(previous: list[RawFetchJob]) -> dict:
    for job in previous:
        if job.summary and "positions" in job.summary:
            return dict(job.summary["positions"])
    return {}


def fetch_directory(ctx: Context) -> Fetched:
    """All streams; a failing stream never stops the others. Returns the summary and the staging files seen."""
    previous = _previous(ctx)
    positions, since = _positions(previous), _since(previous, ctx.now())
    max_pages = int(ctx.options.get("max_pages") or MAX_PAGES)
    logger.info("raw job #%d directory: resume positions %s, since %d", ctx.job_id, sorted(positions), since)
    stager = Stager(ctx.staging)
    crawler = Crawler(stager, ctx.get_raw, ctx.now)
    total = len(STREAMS) + 1
    streams: dict[str, dict] = {}
    for n, stream in enumerate(STREAMS):
        ctx.progress(n, total, f"Abruf {stream.name}")
        info = streams[stream.name] = {"pages": 0, "finished": False}
        try:
            start = positions.get(stream.name) if stream.resumable else None
            for resume, finished in crawler.pages(stream, start, HEAD_PAGES if stream.head else max_pages, since):
                info["pages"] += 1
                info["finished"] = finished
                if stream.resumable:
                    positions[stream.name] = resume
                if info["pages"] % 10 == 0:
                    ctx.progress(n, total, f"Abruf {stream.name}: Seite {info['pages']}")
        except Exception as exc:  # a broken answer deep inside a stream never stops the others
            logger.exception("raw job #%d: stream %s failed", ctx.job_id, stream.name)
            crawler.fail(stream.source, f"{stream.name}: {exc}")
        logger.info("raw job #%d stream %s: %d pages, finished %s", ctx.job_id, stream.name, info["pages"], info["finished"])
    ctx.progress(len(STREAMS), total, "Staging → Rohdaten")
    try:
        raw = load_staging(ctx.factory, stager.root)
        raw = {k: raw[k] for k in ("found", "new", "skipped", "failed")}
    except Exception as exc:  # the staging files stay; the next job loads them
        logger.exception("raw job #%d: staging -> raw failed", ctx.job_id)
        raw = {"error": str(exc)[:200]}
    sources = {s: {"errors": st.errors, "error_details": st.error_details[:10], "requests": st.requests, "staged": st.written}
               for s, st in crawler.stats.items()}
    for s, st in sources.items():
        logger.info("raw job #%d %s: %d requests, %d staging files written, %d errors", ctx.job_id, s, st["requests"], st["staged"], st["errors"])
    return Fetched({"sources": sources, "streams": streams, "positions": positions, "raw": raw}, crawler.files)
