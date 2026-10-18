"""Status logic of the metric catalog.

* Checks run on every data load (``refresh``) and before every release; results are stored with the state.
* A failed check blocks the metric automatically (validated or draft -> blocked).
* ``approve`` releases a metric only when all checks are green (else ``ChecksNotGreen`` -> 409).
* ``block`` blocks a metric manually; it stays blocked until it is released again.
* ``create_basket`` registers a market-basket metric from an Index Manufacture draft (status draft).
* ``create_custom`` registers a market-basket metric built in tab "Metriken" (key derived from the name).
* ``create_structured`` registers a chain / single-market metric from agent step 2 (links, rules, notes; status draft).
Every status change, release, block and check result is logged.
"""

import logging
import re
from dataclasses import dataclass
from datetime import date

from sqlalchemy.orm import Session

from backend.app.fed.metrics import ChainPoint, Computed, build_chains, compute
from backend.app.metrics.basket import basket_points
from backend.app.metrics.catalog import BY_KEY, CHAIN_KINDS, METRICS, MetricDef, MetricPoint, basket_def, chain_def, metric_points
from backend.app.metrics.chain import chain_points
from backend.app.metrics.checks import OK, run_basket_checks, run_chain_checks, run_metric_checks
from backend.app.metrics.liquidity import average_liquidity, fed_volumes, with_chain_liquidity
from backend.app.metrics.models import BLOCKED, DRAFT, VALIDATED, CustomMetric, MetricState
from backend.app.models import utcnow

logger = logging.getLogger(__name__)


class MetricNotFound(KeyError):
    pass


class MetricExists(Exception):
    pass


class ChecksNotGreen(Exception):
    def __init__(self, failing: list[str]):
        super().__init__(", ".join(failing))
        self.failing = failing


@dataclass
class Evaluation:
    defn: MetricDef
    points: list[MetricPoint]
    checks: list[dict]

    @property
    def failing(self) -> list[str]:
        return [c["label"] for c in self.checks if c["status"] != OK]

    @property
    def liquidity(self) -> float | None:
        """Average daily 24h turnover (USD) of the last 30 days with data."""
        return average_liquidity(self.points)


def evaluate(data: Computed, chains: dict[str, dict[str, list[ChainPoint]]], today: date | None = None) -> dict[str, Evaluation]:
    """Series and check results of every catalog metric (pure, nothing stored)."""
    today = today or utcnow().date()
    out, volumes = {}, fed_volumes(data)
    for defn in METRICS:
        points = with_chain_liquidity(defn, volumes, metric_points(defn, chains))
        out[defn.key] = Evaluation(defn, points, run_metric_checks(defn, data, points, today))
    return out


def custom_def(row: CustomMetric) -> MetricDef:
    return chain_def(row) if row.kind in CHAIN_KINDS else basket_def(row)


def evaluate_custom(session: Session, row: CustomMetric, today: date) -> Evaluation:
    """Series and checks of one custom metric: market basket (unchanged) or chain / single market."""
    defn = custom_def(row)
    if defn.is_chain:
        points, violations = chain_points(session, defn, today)
        return Evaluation(defn, points, run_chain_checks(points, violations))
    points, violations = basket_points(session, defn, today)
    return Evaluation(defn, points, run_basket_checks(points, violations))


def evaluate_baskets(session: Session, today: date | None = None) -> dict[str, Evaluation]:
    today = today or utcnow().date()
    return {row.key: evaluate_custom(session, row, today)
            for row in session.query(CustomMetric).order_by(CustomMetric.created_at, CustomMetric.key)}


def evaluate_db(session: Session, today: date | None = None) -> dict[str, Evaluation]:
    data = compute(session)
    return {**evaluate(data, build_chains(data.values, data.sessions), today), **evaluate_baskets(session, today)}


def get_state(session: Session, key: str) -> MetricState:
    state = session.get(MetricState, key)
    if state is None:
        state = MetricState(key=key, status=DRAFT, checks=[])
        session.add(state)
        logger.info("metric %s registered in the catalog (status draft)", key)
    return state


def _set_status(state: MetricState, status: str, reason: str | None, trigger: str) -> None:
    if state.status != status:
        logger.warning("metric %s status %s -> %s (%s): %s", state.key, state.status, status, trigger, reason or "-")
    state.status, state.status_reason = status, reason
    if status == BLOCKED:
        state.blocked_at = utcnow()


def apply(session: Session, ev: Evaluation, trigger: str) -> MetricState:
    """Store the check results; a failed check blocks the metric automatically."""
    state = get_state(session, ev.defn.key)
    state.checks, state.checked_at = ev.checks, utcnow()
    logger.info("metric %s checks (%s): %d days, %s", ev.defn.key, trigger, len(ev.points),
                {c["key"]: f"{c['status']}/{c['count']}" for c in ev.checks})
    if ev.failing and state.status != BLOCKED:
        _set_status(state, BLOCKED, "Automatisch gesperrt: " + ", ".join(ev.failing), f"automatic, {trigger}")
    return state


def refresh(session: Session, trigger: str, today: date | None = None) -> dict[str, Evaluation]:
    """Re-run all checks and apply the automatic status changes (called after every data load)."""
    evaluations = evaluate_db(session, today)
    for ev in evaluations.values():
        apply(session, ev, trigger)
    session.commit()
    return evaluations


def refresh_safely(session: Session, trigger: str) -> None:
    """``refresh`` that never breaks the data load it follows."""
    try:
        refresh(session, trigger)
    except Exception:
        session.rollback()
        logger.exception("metric checks after %s failed", trigger)


def definition(session: Session, key: str) -> MetricDef:
    if key in BY_KEY:
        return BY_KEY[key]
    row = session.get(CustomMetric, key)
    if row is None:
        raise MetricNotFound(key)
    return custom_def(row)


def create_basket(session: Session, key: str, name: str, calculation: str, markets: list[dict],
                  draft_id: int | None = None) -> MetricState:
    """Register a market-basket metric; it always starts as draft (release only via ``approve``)."""
    if key in BY_KEY or session.get(CustomMetric, key) is not None:
        raise MetricExists(key)
    session.add(CustomMetric(key=key, name=name, calculation=calculation, draft_id=draft_id,
                             markets=[{k: m.get(k) for k in ("source", "external_id", "title")} for m in markets]))
    state = get_state(session, key)
    session.commit()
    logger.info("metric %s %r created from draft %s with %d markets (status %s)", key, name, draft_id, len(markets), state.status)
    return state


def custom_key(session: Session, name: str) -> str:
    """Free key ``cm_<slug>`` (suffix _2, _3 ... when taken)."""
    base = "cm_" + (re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")[:40] or "metric")
    key, n = base, 1
    while key in BY_KEY or session.get(CustomMetric, key) is not None:
        n += 1
        key = f"{base}_{n}"
    return key


def create_custom(session: Session, name: str, calculation: str, markets: list[dict]) -> tuple[str, MetricState]:
    """Market-basket metric built by the user in tab "Metriken" (status draft, duplicate markets dropped)."""
    unique = list({(m["source"], m["external_id"]): m for m in markets}.values())
    key = custom_key(session, name)
    logger.info("custom metric %r requested with %d markets -> key %s", name, len(unique), key)
    return key, create_basket(session, key, name, calculation, unique)


def create_structured(session: Session, name: str, kind: str, spec: dict, calculation: str = "",
                      today: date | None = None) -> tuple[str, MetricState, Evaluation]:
    """Chain / single-market metric (agent step 2): stored structured, starts as draft, then the checks run once."""
    if kind not in CHAIN_KINDS or not spec.get("links"):
        raise ValueError(f"structured metric needs kind chain/single and links, got {kind!r}")
    key = custom_key(session, name)
    session.add(CustomMetric(key=key, name=name, calculation=calculation[:4000], kind=kind, spec=spec, markets=[
        {"source": l["source"], "external_id": l["external_id"], "title": l.get("title") or ""} for l in spec["links"]]))
    state = get_state(session, key)
    session.flush()
    logger.info("structured metric %s %r created: kind %s, %d links, rules %s, %d notes (status %s)", key, name, kind,
                len(spec["links"]), spec.get("rules"), len(spec.get("notes") or []), state.status)
    ev = evaluate_custom(session, session.get(CustomMetric, key), today or utcnow().date())
    state = apply(session, ev, "created by agent")
    session.commit()
    return key, state, ev


def approve(session: Session, key: str, today: date | None = None) -> MetricState:
    defn = definition(session, key)
    ev = evaluate_db(session, today)[defn.key]
    state = apply(session, ev, "approve")
    if ev.failing:
        session.commit()
        logger.warning("metric %s release refused: failing checks %s", key, ev.failing)
        raise ChecksNotGreen(ev.failing)
    _set_status(state, VALIDATED, None, "manual release")
    state.validated_at = utcnow()
    session.commit()
    logger.info("metric %s released at %s", key, state.validated_at.isoformat())
    return state


def block(session: Session, key: str, reason: str | None = None) -> MetricState:
    definition(session, key)
    state = get_state(session, key)
    _set_status(state, BLOCKED, "Manuell gesperrt" + (f": {reason}" if reason else ""), "manual block")
    session.commit()
    logger.info("metric %s blocked manually (%s)", key, reason or "ohne Grund")
    return state
