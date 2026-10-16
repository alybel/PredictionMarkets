"""Metric catalog endpoints (tab "Metriken").

GET  /metrics               -> every metric: definition, status, release/block date, check results, liquidity, daily series
                               (optional ?min_liquidity=USD: only metrics whose 30-day average turnover reaches it;
                               liquidity_median = median of the current liquidity of all metrics, reference for the filter)
POST /metrics               -> create a market-basket metric {name, calculation, markets: [{source, external_id, title}]}
                               (201, status draft; 422 on invalid input)
POST /metrics/check         -> re-run all checks (automatic block on a failed check)
POST /metrics/{key}/approve -> release; 409 unless all checks are green, 404 for an unknown metric
POST /metrics/{key}/block   -> block manually (optional body {"reason": "..."})
"""

import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from backend.app.deps import get_session
from backend.app.metrics.liquidity import median_liquidity, passes
from backend.app.metrics.models import MetricState
from backend.app.metrics.service import (ChecksNotGreen, Evaluation, MetricNotFound, apply, approve, block, create_custom,
                                         evaluate_db, get_state, refresh)

logger = logging.getLogger(__name__)
router = APIRouter(tags=["metrics"])


class BlockRequest(BaseModel):
    reason: str | None = None


class MarketRef(BaseModel):
    source: str = Field(pattern="^(polymarket|kalshi)$")
    external_id: str = Field(min_length=1, max_length=256)
    title: str = Field(default="", max_length=500)


class CreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    calculation: str = Field(default="", max_length=4000)
    markets: list[MarketRef] = Field(min_length=1, max_length=50)


def _iso(v: datetime | None) -> str | None:
    return v.isoformat() if v else None


def _payload(ev: Evaluation, state: MetricState) -> dict:
    d = ev.defn
    green = bool(state.checks) and all(c["status"] == "ok" for c in state.checks)
    return {"key": d.key, "name": d.name, "unit": d.unit, "source": d.source, "calculation": d.calculation,
            "status": state.status, "status_reason": state.status_reason, "validated_at": _iso(state.validated_at),
            "blocked_at": _iso(state.blocked_at), "checked_at": _iso(state.checked_at), "checks": state.checks,
            "all_green": green, "can_approve": green and state.status != "validated", "liquidity_usd": _usd(ev.liquidity),
            "series": [{"date": p.day.isoformat(), "value": round(p.value, 3), "roll": p.roll, "liquidity": _usd(p.liquidity)}
                       for p in ev.points]}


def _usd(v: float | None) -> float | None:
    return round(v, 2) if v is not None else None


@router.get("/metrics")
def overview(min_liquidity: float | None = Query(None, ge=0), session=Depends(get_session)) -> dict:
    evaluations = evaluate_db(session)
    states = {}
    for key, ev in evaluations.items():
        state = session.get(MetricState, key)
        states[key] = apply(session, ev, "first view") if state is None or state.checked_at is None else state
    session.commit()
    shown = [k for k, ev in evaluations.items() if passes(ev.liquidity, min_liquidity)]
    logger.info("metric overview (min liquidity %s): %d of %d shown, %s", min_liquidity, len(shown), len(evaluations),
                {k: s.status for k, s in states.items()})
    return {"metrics": [_payload(evaluations[k], states[k]) for k in shown], "min_liquidity": min_liquidity,
            "liquidity_median": median_liquidity(ev.liquidity for ev in evaluations.values())}


@router.post("/metrics", status_code=201)
def create_metric(body: CreateRequest, session=Depends(get_session)) -> dict:
    name = body.name.strip()
    if not name:
        raise HTTPException(422, "Name darf nicht leer sein.")
    logger.info("metric creation requested via API: %r with %d markets", name, len(body.markets))
    key, state = create_custom(session, name, body.calculation.strip(), [m.model_dump() for m in body.markets])
    return {"key": key, "name": name, "status": state.status}


@router.post("/metrics/check")
def check(session=Depends(get_session)) -> dict:
    logger.info("metric checks requested via API")
    evaluations = refresh(session, "manual check")
    return {"metrics": [_payload(ev, get_state(session, k)) for k, ev in evaluations.items()],
            "liquidity_median": median_liquidity(ev.liquidity for ev in evaluations.values())}


@router.post("/metrics/{key}/approve")
def approve_metric(key: str, session=Depends(get_session)) -> dict:
    logger.info("metric %s release requested via API", key)
    try:
        state = approve(session, key)
    except MetricNotFound:
        raise HTTPException(404, f"Unbekannte Metrik: {key}") from None
    except ChecksNotGreen as exc:
        raise HTTPException(409, "Freigabe abgelehnt, Prüfungen nicht grün: " + ", ".join(exc.failing)) from None
    return {"key": key, "status": state.status, "validated_at": _iso(state.validated_at)}


@router.post("/metrics/{key}/block")
def block_metric(key: str, body: BlockRequest | None = None, session=Depends(get_session)) -> dict:
    logger.info("metric %s block requested via API", key)
    try:
        state = block(session, key, body.reason if body else None)
    except MetricNotFound:
        raise HTTPException(404, f"Unbekannte Metrik: {key}") from None
    return {"key": key, "status": state.status, "status_reason": state.status_reason, "blocked_at": _iso(state.blocked_at)}
