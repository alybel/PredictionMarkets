"""Fed rate chain endpoints (section "Fed-Zinsketten" of the tab "Datenqualität").

POST /fed/load                -> load missing markets and days of both sources into the raw layer
POST /fed/staging/fetch       -> fetch both sources unchanged into the staging folder (backend.app.fed.fetch)
POST /fed/staging/load        -> load new staging files 1:1 into the raw layer (backend.app.staging.raw_loader)
GET  /quality/fed/coverage    -> per meeting and source: first/last day, days with value, gaps
GET  /quality/fed/chains      -> front and second chain per series (kalshi, polymarket, combined), roll days
GET  /quality/fed/checks      -> probability sum, missing days, source divergence, jumps, values after close
GET  /quality/fed/day/{date}  -> raw outcome prices behind every meeting value of one day (drill-down)

Both loads into the raw layer re-run the metric catalog checks (backend.app.metrics.service).
"""

import logging
from datetime import date

from fastapi import APIRouter, Depends, HTTPException

from backend.app.db import make_session_factory
from backend.app.deps import get_session
from backend.app.fed.checks import gap_ranges, missing_days, run_checks
from backend.app.fed.loader import load_fed
from backend.app.fed.metrics import CHAINS, SERIES, SOURCES, before_decision, build_chains, chain_session, compute
from backend.app.fed.fetch import fetch_fed
from backend.app.fed.sources import HttpFedSource, http_get_raw
from backend.app.metrics.service import refresh_safely
from backend.app.staging.raw_loader import load_staging

logger = logging.getLogger(__name__)
router = APIRouter(tags=["fed"])


def get_fed_source():
    """Market source as a dependency so tests never reach the network."""
    return HttpFedSource()


def get_raw_getter():
    """Raw HTTP getter of the staging fetch as a dependency so tests never reach the network."""
    return http_get_raw


def get_staging_dir():
    """Staging folder; None = default ``data/staging`` (overridden in tests)."""
    return None


def _run(step: str, fn) -> dict:
    """Run one staging step; any error becomes a 500 whose detail names the cause (shown in the frontend)."""
    logger.info("fed staging %s requested via API", step)
    try:
        summary = fn()
    except Exception as exc:
        logger.exception("fed staging %s failed", step)
        raise HTTPException(500, f"{step} fehlgeschlagen: {type(exc).__name__}: {exc}"[:500]) from exc
    return {k: v for k, v in summary.items() if k != "failures"} | {"failures": summary.get("failures", [])[:20]}


def _iso(d: date | None) -> str | None:
    return d.isoformat() if d else None


def _r(v: float | None) -> float | None:
    return None if v is None else round(v, 3)


@router.post("/fed/load")
def load(session=Depends(get_session), source=Depends(get_fed_source)) -> dict:
    logger.info("fed load requested via API")
    summary = load_fed(session, source)
    refresh_safely(session, "fed load")  # metric catalog: checks run again on every data load
    return summary


@router.post("/fed/staging/fetch")
def staging_fetch(raw_getter=Depends(get_raw_getter), root=Depends(get_staging_dir)) -> dict:
    return _run("Abrufen -> Staging", lambda: fetch_fed(root, raw_getter))


@router.post("/fed/staging/load")
def staging_load(session=Depends(get_session), root=Depends(get_staging_dir)) -> dict:
    summary = _run("Staging -> Raw", lambda: load_staging(make_session_factory(session.get_bind()), root))
    refresh_safely(session, "staging load")
    return summary


@router.get("/quality/fed/coverage")
def coverage(session=Depends(get_session)) -> dict:
    data = compute(session)
    values, sessions = data.values, data.sessions
    per: dict[tuple[date, str], set[date]] = {}
    for (s, m, d) in values:
        if s in SOURCES:
            per.setdefault((m, s), set()).add(d)
    rows = []
    for (m, s), days in sorted(per.items()):
        gaps = gap_ranges(missing_days(days))
        rows.append({"session_date": m.isoformat(), "source": s, "first_date": min(days).isoformat(), "last_date": max(days).isoformat(),
                     "days": len(days), "gaps": [{"from": a.isoformat(), "to": b.isoformat(), "days": (b - a).days + 1} for a, b in gaps]})
    all_days = [d for (_, _, d) in values]
    logger.info("fed coverage: %d meeting/source rows, %d with gaps", len(rows), sum(bool(r["gaps"]) for r in rows))
    return {"first_date": _iso(min(all_days, default=None)), "last_date": _iso(max(all_days, default=None)),
            "sessions": [m.isoformat() for m in sessions], "rows": rows}


@router.get("/quality/fed/chains")
def chains(session=Depends(get_session)) -> dict:
    data = compute(session)
    built = build_chains(data.values, data.sessions)
    series = {k: {name: [{"date": p.day.isoformat(), "session_date": p.session_date.isoformat(), "value": _r(p.value),
                          "change": _r(p.change), "roll": p.roll, "linked": _r(p.linked)} for p in built[k][name]] for name in CHAINS}
              for k in SERIES}
    rolls = {k: [{"date": p["date"], "chain": name, "session_date": p["session_date"]} for name in CHAINS for p in series[k][name] if p["roll"]]
             for k in SERIES}
    return {"sessions": [m.isoformat() for m in data.sessions], "series": series, "rolls": rolls}


@router.get("/quality/fed/checks")
def checks(session=Depends(get_session)) -> list[dict]:
    data = compute(session)
    return run_checks(data, build_chains(data.values, data.sessions))


@router.get("/quality/fed/day/{day}")
def day_detail(day: date, session=Depends(get_session)) -> dict:
    data = compute(session)
    values, sessions = data.values, data.sessions
    todays = [p for p in data.points if p.day == day and p.day <= p.session_date]
    if not todays:
        raise HTTPException(404, f"no fed prices stored for {day.isoformat()}")
    front, second = chain_session(sessions, day, 0), chain_session(sessions, day, 1)
    out = []
    for m in sorted({p.session_date for p in todays}):
        sources = {}
        for s in SOURCES:
            rows = sorted((p for p in todays if p.session_date == m and p.source == s), key=lambda p: p.outcome_bp)
            if not rows:
                continue
            v = values.get((s, m, day))
            raw = sum(p.price for p in rows)
            sources[s] = {"expected_bp": v.expected_bp if v else None, "raw_sum": raw, "rejected": (s, m, day) in data.rejected, "outcomes": [
                {"external_id": p.external_id, "label": p.outcome_label, "bp": p.outcome_bp, "price": p.price,
                 "probability": p.price / raw if raw > 0 else None, "volume_usd": p.volume_usd,
                 "observed_at": p.observed_at.isoformat(), "origin": p.origin, "used": before_decision(p)} for p in rows]}
        combined = values.get(("combined", m, day))
        out.append({"session_date": m.isoformat(), "chain": "front" if m == front else "second" if m == second else None,
                    "sources": sources, "combined_bp": combined.expected_bp if combined else None})
    logger.info("fed day %s: %d meetings, %d raw prices", day, len(out), len(todays))
    return {"date": day.isoformat(), "front_session": _iso(front), "second_session": _iso(second), "sessions": out}
