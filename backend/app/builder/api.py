"""Index builder endpoints: indices composed only of validated catalog metrics.

GET  /builder/metrics         -> selection: validated metrics only (draft and blocked never appear), with liquidity
                                 (optional ?min_liquidity=USD on the 30-day average turnover; liquidity_median of all
                                 validated metrics as reference for the filter)
GET  /builder/indices         -> metric indices with their components and a warning flag
POST /builder/indices         -> create (201); 400 invalid components, 409 name taken or metric not validated
GET  /builder/indices/{name}  -> standardized index series, components, warnings (?days, default 365)
PUT  /builder/indices/{name}  -> replace the components (same rules as create); 404 unknown, 409 archived rule index
DELETE /builder/indices/{name} -> delete (204); 404 unknown

Rule-based indices of /indices are the archive (read only, but deletable) (see backend.app.api).
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field

from backend.app.builder.models import MetricIndex
from backend.app.builder.service import Component, InvalidComponents, NotValidated, index_payload, validate_components
from backend.app.deps import get_session
from backend.app.engine import get_rule
from backend.app.metrics.liquidity import median_liquidity, passes
from backend.app.metrics.models import VALIDATED, MetricState
from backend.app.metrics.service import evaluate_db

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/builder", tags=["builder"])


class ComponentBody(BaseModel):
    key: str = Field(min_length=1, max_length=64)
    weight: float = 1.0


class IndexBody(BaseModel):
    components: list[ComponentBody] = Field(default_factory=list, max_length=50)


class IndexCreateBody(IndexBody):
    name: str = Field(min_length=1, max_length=128)


def _components(session, name: str, body: IndexBody, action: str) -> list[dict]:
    try:
        return validate_components(session, [Component(c.key, c.weight) for c in body.components])
    except InvalidComponents as exc:
        logger.warning("metric index %r %s refused (400): %s", name, action, exc)
        raise HTTPException(400, str(exc)) from None
    except NotValidated as exc:
        logger.warning("metric index %r %s refused (409): %s", name, action, exc)
        raise HTTPException(409, str(exc)) from None


def _stored(session, name: str) -> MetricIndex:
    row = session.query(MetricIndex).filter_by(name=name).first()
    if row is None:
        raise HTTPException(404, f"Unbekannter Index: {name}")
    return row


def _row_payload(session, row: MetricIndex) -> dict:
    states = {c["key"]: session.get(MetricState, c["key"]) for c in row.components}
    return {"name": row.name, "components": row.components,
            "warning": any(s is None or s.status != VALIDATED for s in states.values())}


@router.get("/metrics")
def selectable_metrics(min_liquidity: float | None = Query(None, ge=0), session=Depends(get_session)) -> dict:
    evaluations = evaluate_db(session)
    states = {k: session.get(MetricState, k) for k in evaluations}
    validated = [k for k in evaluations if states[k] is not None and states[k].status == VALIDATED]
    items = [{"key": k, "name": ev.defn.name, "unit": ev.defn.unit, "source": ev.defn.source,
              "validated_at": states[k].validated_at.isoformat() if states[k].validated_at else None,
              "days": len(ev.points), "last_value": round(ev.points[-1].value, 4) if ev.points else None,
              "liquidity_usd": round(ev.liquidity, 2) if ev.liquidity is not None else None}
             for k, ev in ((k, evaluations[k]) for k in validated) if passes(ev.liquidity, min_liquidity)]
    med = median_liquidity(evaluations[k].liquidity for k in validated)
    logger.info("builder selection: %d validated of %d metrics (%s), min liquidity %s, median %s", len(items),
                len(evaluations), [i["key"] for i in items], min_liquidity, med)
    return {"metrics": items, "liquidity_median": med}


@router.get("/indices")
def list_indices(session=Depends(get_session)) -> dict:
    rows = session.query(MetricIndex).order_by(MetricIndex.name).all()
    return {"indices": [_row_payload(session, r) for r in rows]}


@router.post("/indices", status_code=201)
def create_index(body: IndexCreateBody, session=Depends(get_session)) -> dict:
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "Name darf nicht leer sein.")
    logger.info("metric index %r create requested: %s", name, [(c.key, c.weight) for c in body.components])
    if session.query(MetricIndex).filter_by(name=name).first() or get_rule(session, name):
        logger.warning("metric index %r create refused (409): name exists", name)
        raise HTTPException(409, f"Index {name!r} existiert bereits.")
    row = MetricIndex(name=name, components=_components(session, name, body, "create"))
    session.add(row)
    session.commit()
    logger.info("metric index %r saved with %s", name, row.components)
    return _row_payload(session, row)


@router.get("/indices/{name}")
def read_index(name: str, days: int = Query(365, ge=1, le=3650), session=Depends(get_session)) -> dict:
    return index_payload(session, _stored(session, name), evaluate_db(session), days)


@router.put("/indices/{name}")
def update_index(name: str, body: IndexBody, session=Depends(get_session)) -> dict:
    logger.info("metric index %r update requested: %s", name, [(c.key, c.weight) for c in body.components])
    if get_rule(session, name) is not None:
        logger.warning("metric index %r update refused (409): archived rule index", name)
        raise HTTPException(409, f"Index {name!r} ist ein Archiv-Index (regelbasiert) und nur lesbar.")
    row = _stored(session, name)
    row.components = _components(session, name, body, "update")
    session.commit()
    logger.info("metric index %r updated to %s", name, row.components)
    return _row_payload(session, row)


@router.delete("/indices/{name}", status_code=204)
def delete_index(name: str, session=Depends(get_session)) -> Response:
    logger.info("metric index %r delete requested", name)
    row = _stored(session, name)
    session.delete(row)
    session.commit()
    logger.info("metric index %r deleted (components %s)", name, row.components)
    return Response(status_code=204)
