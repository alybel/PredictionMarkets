"""Metric-based index: validation of the components and the standardized index series.

* Only ``validated`` metrics may be saved into an index (else ``NotValidated`` -> 409).
* Standardization: each metric becomes an expanding z-score — day t uses only that metric's values up to t
  (no look-ahead) — so bp and probabilities become unit-free and comparable. The first ``MIN_HISTORY`` days
  of a metric have no z-score yet.
* Index value of a day = weighted mean of the components' z-scores; only days on which every component has one.
* A component blocked after saving keeps being computed; the index then carries a warning (logged).
"""

import logging
from dataclasses import dataclass
from statistics import fmean, pstdev

from sqlalchemy.orm import Session

from backend.app.builder.models import MetricIndex
from backend.app.metrics.catalog import MetricPoint
from backend.app.metrics.models import DRAFT, VALIDATED, MetricState
from backend.app.metrics.service import Evaluation, MetricNotFound, definition

logger = logging.getLogger(__name__)

MIN_HISTORY = 5


class InvalidComponents(ValueError):
    pass


class NotValidated(Exception):
    pass


@dataclass(frozen=True)
class Component:
    key: str
    weight: float


def status_of(session: Session, key: str) -> MetricState | None:
    return session.get(MetricState, key)


def validate_components(session: Session, components: list[Component]) -> list[dict]:
    """Cleaned components; ``InvalidComponents`` (400) on shape errors, ``NotValidated`` (409) on draft/blocked metrics."""
    if not components:
        raise InvalidComponents("Ein Index braucht mindestens eine Metrik.")
    keys = [c.key for c in components]
    if len(set(keys)) != len(keys):
        raise InvalidComponents("Jede Metrik darf nur einmal vorkommen.")
    for c in components:
        if not c.weight > 0:
            raise InvalidComponents(f"Gewicht von {c.key} muss größer als 0 sein.")
        try:
            definition(session, c.key)
        except MetricNotFound:
            raise InvalidComponents(f"Unbekannte Metrik: {c.key}") from None
        state = status_of(session, c.key)
        status = state.status if state else DRAFT
        if status != VALIDATED:
            raise NotValidated(f"Metrik {c.key} ist nicht freigegeben (Status {status}); nur validierte Metriken sind erlaubt.")
    return [{"key": c.key, "weight": float(c.weight)} for c in components]


def standardize(points: list[MetricPoint], min_history: int = MIN_HISTORY) -> dict:
    """Expanding z-score per day (oldest first in, day -> z out); a flat history gives 0."""
    out, values = {}, []
    for p in points:
        values.append(p.value)
        if len(values) >= min_history:
            sd = pstdev(values)
            out[p.day] = (p.value - fmean(values)) / sd if sd > 0 else 0.0
    return out


def index_series(z_by_key: dict[str, dict], weights: dict[str, float]) -> list[dict]:
    """Weighted mean of the z-scores on every day all components have one."""
    if not z_by_key:
        return []
    days = sorted(set.intersection(*(set(z) for z in z_by_key.values())))
    total = sum(weights.values())
    return [{"date": d.isoformat(), "value": round(sum(weights[k] * z[d] for k, z in z_by_key.items()) / total, 4),
             "components": {k: round(z[d], 4) for k, z in z_by_key.items()}} for d in days]


def _summary(points: list[dict]) -> dict:
    values = [p["value"] for p in points]
    if not values:
        return {"first": None, "last": None, "change": None, "min": None, "max": None}
    return {"first": values[0], "last": values[-1], "change": round(values[-1] - values[0], 4),
            "min": min(values), "max": max(values)}


def index_payload(session: Session, row: MetricIndex, evaluations: dict[str, Evaluation], days: int) -> dict:
    components, warnings, z_by_key, weights = [], [], {}, {}
    for c in row.components:
        key, ev, state = c["key"], evaluations.get(c["key"]), status_of(session, c["key"])
        status = state.status if state else DRAFT
        if ev is None:
            warnings.append(f"Metrik {key} existiert nicht mehr im Katalog und fließt nicht ein.")
            components.append({"key": key, "name": key, "unit": None, "weight": c["weight"], "status": "unknown"})
            continue
        if status != VALIDATED:
            reason = state.status_reason if state and state.status_reason else "ohne Grund"
            warnings.append(f"Metrik „{ev.defn.name}“ ist {'gesperrt' if status == 'blocked' else status} ({reason}). "
                            "Der Index rechnet weiter; bitte Zusammensetzung prüfen.")
        z_by_key[key], weights[key] = standardize(ev.points), c["weight"]
        last = ev.points[-1] if ev.points else None
        components.append({"key": key, "name": ev.defn.name, "unit": ev.defn.unit, "weight": c["weight"], "status": status,
                           "status_reason": state.status_reason if state else None,
                           "blocked_at": state.blocked_at.isoformat() if state and state.blocked_at else None,
                           "last_value": round(last.value, 4) if last else None,
                           "last_date": last.day.isoformat() if last else None, "days": len(ev.points),
                           "liquidity_usd": round(ev.liquidity, 2) if ev.liquidity is not None else None})
    points = index_series(z_by_key, weights)[-days:]
    if warnings:
        logger.warning("metric index %r computed with warnings: %s", row.name, " | ".join(warnings))
    logger.info("metric index %r: %d components, %d days, last %s", row.name, len(components), len(points),
                points[-1]["value"] if points else None)
    return {"name": row.name, "components": components, "warning": bool(warnings), "warnings": warnings,
            "points": points, "summary": _summary(points), "min_history": MIN_HISTORY,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None}
