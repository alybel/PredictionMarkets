"""REST endpoints of the Economic Uncertainty Index.

Public (only the computed value, sources named, no raw data):
GET  /eui                        -> latest value
GET  /eui/history?days=90        -> daily values
GET  /eui/plausibility           -> event-day check (stands in for the backtest)

Admin (human review and operations):
GET  /eui/admin/classifications  -> proposals to review (?status=proposed&economic_only=1)
PUT  /eui/admin/classifications/{id} -> confirm / reject / change polarity
GET  /eui/admin/constituents     -> constituents of the latest value with weights
GET  /eui/admin/runs             -> recent snapshot runs
POST /eui/admin/run              -> run the daily pipeline now
POST /eui/admin/events           -> add an event date for the plausibility check
"""

import logging
from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select

from backend.app.deps import get_session
from backend.app.eui.classify import classifier_from_env
from backend.app.eui.compute import history, latest_value
from backend.app.eui.daily import run_daily
from backend.app.eui.models import POLARITIES, STATUSES, IndexComposition, IndexValue, MarketClassification
from backend.app.eui.plausibility import add_event, check_plausibility
from backend.app.eui.snapshot import default_clients, recent_runs
from backend.app.models import utcnow

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/eui", tags=["eui"])

SOURCES = ["Polymarket", "Kalshi"]
ATTRIBUTION = "Economic Uncertainty Index - Quelle: Polymarket und Kalshi"


def get_clients() -> list:
    return default_clients()


def get_classifier():
    return classifier_from_env()


class ClassificationUpdate(BaseModel):
    status: str | None = None
    polarity: str | None = None
    is_economic: bool | None = None


class EventCreate(BaseModel):
    event_date: date
    label: str
    kind: str = "geopolitical"


def value_payload(row: IndexValue | None) -> dict:
    return {
        "value_date": row.value_date.isoformat() if row else None,
        "value": row.value if row else None,
        "constituent_count": row.constituent_count if row else 0,
        "as_of": row.computed_at.isoformat() if row else None,
        "sources": SOURCES,
        "attribution": ATTRIBUTION,
    }


@router.get("")
def current(session=Depends(get_session)) -> dict:
    return value_payload(latest_value(session))


@router.get("/history")
def value_history(days: int = 90, session=Depends(get_session)) -> list[dict]:
    days = max(1, min(days, 3650))
    return [{"value_date": r.value_date.isoformat(), "value": r.value, "constituent_count": r.constituent_count} for r in history(session, days)]


@router.get("/plausibility")
def plausibility(session=Depends(get_session)) -> dict:
    return check_plausibility(session)


def classification_payload(row: MarketClassification) -> dict:
    return {
        "id": row.id, "source": row.source, "external_id": row.external_id, "title": row.title,
        "is_economic": row.is_economic, "polarity": row.polarity, "rationale": row.rationale,
        "model": row.model, "status": row.status, "created_at": row.created_at.isoformat() if row.created_at else None,
        "reviewed_at": row.reviewed_at.isoformat() if row.reviewed_at else None,
    }


@router.get("/admin/classifications")
def classifications(status: str = "proposed", economic_only: bool = True, limit: int = 200, session=Depends(get_session)) -> list[dict]:
    stmt = select(MarketClassification).order_by(MarketClassification.id.desc()).limit(max(1, min(limit, 1000)))
    if status:
        stmt = stmt.where(MarketClassification.status == status)
    if economic_only:
        stmt = stmt.where(MarketClassification.is_economic.is_(True))
    return [classification_payload(r) for r in session.scalars(stmt)]


@router.put("/admin/classifications/{classification_id}")
def review_classification(classification_id: int, body: ClassificationUpdate, session=Depends(get_session)) -> dict:
    row = session.get(MarketClassification, classification_id)
    if row is None:
        raise HTTPException(404, f"unknown classification {classification_id}")
    if body.status is not None and body.status not in STATUSES:
        raise HTTPException(400, f"status must be one of {', '.join(STATUSES)}")
    if body.polarity is not None and body.polarity not in POLARITIES:
        raise HTTPException(400, f"polarity must be one of {', '.join(POLARITIES)}")
    if body.status is not None:
        row.status = body.status
    if body.polarity is not None:
        row.polarity = body.polarity
    if body.is_economic is not None:
        row.is_economic = body.is_economic
    row.reviewed_at = utcnow()
    session.commit()
    logger.info("classification %d reviewed: status=%s polarity=%s economic=%s", row.id, row.status, row.polarity, row.is_economic)
    return classification_payload(row)


@router.get("/admin/constituents")
def constituents(session=Depends(get_session)) -> dict:
    row = latest_value(session)
    if row is None:
        return {"value_date": None, "period": None, "constituents": []}
    composition = session.get(IndexComposition, row.composition_id) if row.composition_id else None
    return {"value_date": row.value_date.isoformat(), "period": composition.period if composition else None, "constituents": row.details}


@router.get("/admin/runs")
def runs(session=Depends(get_session)) -> list[dict]:
    return [
        {
            "id": r.id, "snapshot_date": r.snapshot_date.isoformat(), "status": r.status, "source_counts": r.source_counts,
            "eligible_count": r.eligible_count, "started_at": r.started_at.isoformat() if r.started_at else None,
            "finished_at": r.finished_at.isoformat() if r.finished_at else None, "note": r.note,
        }
        for r in recent_runs(session)
    ]


@router.post("/admin/run")
def run_now(classify: bool = True, limit: int | None = None, session=Depends(get_session),
            clients=Depends(get_clients), classifier=Depends(get_classifier)) -> dict:
    logger.info("manual daily run requested (classify=%s, limit=%s)", classify, limit)
    return run_daily(session, clients=clients, classifier=classifier, classify=classify, limit=limit)


@router.post("/admin/events", status_code=201)
def create_event(body: EventCreate, session=Depends(get_session)) -> dict:
    label = body.label.strip()
    if not label:
        raise HTTPException(400, "label must not be empty")
    event = add_event(session, body.event_date, label[:128], body.kind.strip()[:32] or "other")
    return {"id": event.id, "event_date": event.event_date.isoformat(), "label": event.label, "kind": event.kind}
