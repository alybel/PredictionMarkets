"""Status of the five workshop steps of one risk topic, read only from the tables of tickets 1-6 (nothing is stored here).

Steps: 1 raw data (market directory + inbox), 2 topic review list, 3 chain metrics of the topic, 4 weighted index,
5 forecast report. Every step has the same rule: ``open`` without any progress, ``done`` when its goal is reached
and no task is left, ``in_progress`` otherwise. ``ready`` says whether the step can be worked on (its input exists).

A metric belongs to the topic when its structured spec names the topic (``spec.topic.id``, set on acceptance in 4/7);
an index belongs to the topic when one of its components is such a metric.
"""

import logging

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.app.builder.models import MetricIndex
from backend.app.builder.weighting import MANUAL, METHODS
from backend.app.directory.models import DirectoryMarket, DirectoryRun
from backend.app.forecast.models import ForecastReport
from backend.app.metrics.models import BLOCKED, DRAFT, VALIDATED, CustomMetric, MetricState
from backend.app.risk.models import ACCEPTED, PROPOSED, REJECTED, RiskAssessment, RiskTopic
from backend.app.risk.service import prefilter, topic_payload

logger = logging.getLogger(__name__)

OPEN, IN_PROGRESS, DONE = "open", "in_progress", "done"
STEP_TITLES = {1: "Rohdaten", 2: "Themen-Einordnung & Prüfliste", 3: "Ketten-Metriken", 4: "Gewichtung", 5: "Prognosekraft"}


def _status(progress: bool, goal: bool, tasks: list[dict]) -> str:
    if goal and not any(t["count"] for t in tasks):
        return DONE
    return IN_PROGRESS if progress or goal else OPEN


def _task(key: str, label: str, count: int) -> dict:
    return {"key": key, "label": label, "count": int(count)}


def _step(n: int, status: str, ready: bool, tasks: list[dict], counts: dict, ai: str) -> dict:
    return {"step": n, "title": STEP_TITLES[n], "status": status, "ready": ready, "tasks": [t for t in tasks if t["count"]],
            "counts": counts, "ai": ai}


def _raw_step(session: Session, topic: RiskTopic) -> dict:
    d = DirectoryMarket
    total = session.scalar(select(func.count()).select_from(d)) or 0
    inbox = session.scalar(select(func.count()).select_from(d).where(d.is_new.is_(True), d.reviewed_at.is_(None))) or 0
    query = prefilter(session, topic)
    candidates = session.scalar(select(func.count()).select_from(query.subquery())) if query is not None else 0
    last = session.scalar(select(DirectoryRun.finished_at).where(DirectoryRun.finished_at.is_not(None)).order_by(DirectoryRun.id.desc()).limit(1))
    tasks = [_task("inbox", "neue Märkte im Eingangskorb", inbox)]
    counts = {"markets": total, "candidates": candidates or 0, "inbox": inbox, "last_run": last.isoformat() if last else None}
    return _step(1, _status(last is not None, total > 0, tasks), True, tasks, counts,
                 "Kein KI-Schritt – neue Märkte bestätigst du im Eingangskorb als gesehen.")


def _review_step(session: Session, topic: RiskTopic, raw_ready: bool) -> dict:
    a = RiskAssessment
    rows = dict(session.execute(select(a.status, func.count()).where(a.topic_id == topic.id).group_by(a.status)).all())
    unreviewed = session.scalar(select(func.count()).select_from(a).where(a.topic_id == topic.id, a.status == PROPOSED,
                                                                          a.relevant.is_(True))) or 0
    query = prefilter(session, topic)
    assessed = select(a.market_id).where(a.topic_id == topic.id)
    unclassified = session.scalar(select(func.count()).select_from(query.where(DirectoryMarket.id.not_in(assessed)).subquery())) \
        if query is not None else 0
    accepted, rejected = rows.get(ACCEPTED, 0), rows.get(REJECTED, 0)
    tasks = [_task("unreviewed", "ungeprüfte relevante Märkte (KI-Vorschlag)", unreviewed),
             _task("unclassified", "noch nicht eingestufte Kandidaten", unclassified or 0)]
    counts = {"proposed": rows.get(PROPOSED, 0), "unreviewed": unreviewed, "unclassified": unclassified or 0, "accepted": accepted,
              "rejected": rejected}
    return _step(2, _status(rejected > 0, accepted > 0, tasks), raw_ready, tasks, counts,
                 "Haiku schlägt je Markt Relevanz und Lesart vor – wirksam erst nach deinem „Annehmen“.")


def topic_metrics(session: Session, topic: RiskTopic) -> dict[str, dict]:
    """Metric key -> {key, name, kind, status} for the structured metrics accepted for this topic."""
    rows = [m for m in session.scalars(select(CustomMetric).where(CustomMetric.spec.is_not(None)).order_by(CustomMetric.key))
            if ((m.spec or {}).get("topic") or {}).get("id") == topic.id]
    return {m.key: {"key": m.key, "name": m.name, "kind": m.kind, "status": s.status if (s := session.get(MetricState, m.key)) else DRAFT}
            for m in rows}


def topic_indices(session: Session, metrics: dict[str, dict]) -> list[MetricIndex]:
    return [r for r in session.scalars(select(MetricIndex).order_by(MetricIndex.name)) if any(c["key"] in metrics for c in r.components)]


def _metric_step(metrics: dict[str, dict], ready: bool) -> dict:
    by = {s: sum(1 for v in metrics.values() if v["status"] == s) for s in (DRAFT, VALIDATED, BLOCKED)}
    tasks = [_task("drafts", "Metrik-Entwürfe warten auf deine Freigabe", by[DRAFT])]
    counts = {"metrics": len(metrics), "draft": by[DRAFT], "validated": by[VALIDATED], "blocked": by[BLOCKED], "list": list(metrics.values())}
    return _step(3, _status(bool(metrics), by[VALIDATED] > 0, tasks), ready, tasks, counts,
                 "Sonnet schlägt Einzelmärkte und Ketten aus den angenommenen Märkten vor – erst „Übernehmen“ legt einen Entwurf an, "
                 "erst „Freigeben“ macht ihn nutzbar.")


def _index_step(session: Session, indices: list[MetricIndex], ready: bool) -> dict:
    unvalidated = sum(1 for r in indices if any(getattr(session.get(MetricState, c["key"]), "status", DRAFT) != VALIDATED for c in r.components))
    tasks = [_task("unvalidated", "Index enthält nicht freigegebene Metrik", unvalidated)]
    counts = {"indices": [{"name": r.name, "weighting": r.weighting or MANUAL, "weighting_label": METHODS.get(r.weighting or MANUAL, ""),
                           "components": [c["key"] for c in r.components]} for r in indices]}
    return _step(4, _status(False, bool(indices), tasks), ready, tasks, counts,
                 "Sonnet schlägt Verfahren und Gewichte vor – gespeichert wird erst mit „Übernehmen & Index speichern“.")


def _forecast_step(session: Session, indices: list[MetricIndex], ready: bool) -> dict:
    names = [r.name for r in indices]
    reports = {r.index_name: r for r in session.scalars(select(ForecastReport).where(ForecastReport.index_name.in_(names)))} if names else {}
    missing = [n for n in names if n not in reports]
    tasks = [_task("missing", "Index ohne Prognosekraft-Bericht", len(missing))]
    counts = {"reports": [{"index": n, "computed_at": reports[n].computed_at.isoformat() if reports[n].computed_at else None,
                           "lights": {t["ticker"]: t["light"] for t in (reports[n].payload or {}).get("targets", [])}} for n in names if n in reports],
              "missing": missing}
    return _step(5, _status(bool(reports), bool(reports), tasks), ready, tasks, counts,
                 "Kein KI-Schritt – der Bericht wird regelbasiert gerechnet (beim Speichern und täglich).")


def workshop_status(session: Session, topic: RiskTopic) -> dict:
    raw = _raw_step(session, topic)
    review = _review_step(session, topic, raw["counts"]["markets"] > 0)
    metrics = topic_metrics(session, topic)
    indices = topic_indices(session, metrics)
    steps = [raw, review, _metric_step(metrics, review["counts"]["accepted"] > 0),
             _index_step(session, indices, any(v["status"] == VALIDATED for v in metrics.values())), _forecast_step(session, indices, bool(indices))]
    current = next((s["step"] for s in steps if s["status"] != DONE), len(steps))
    logger.info("workshop status of topic %r: %s, open tasks %s, current step %d", topic.name, {s["step"]: s["status"] for s in steps},
                {s["step"]: {t["key"]: t["count"] for t in s["tasks"]} for s in steps if s["tasks"]}, current)
    return {"topic": topic_payload(topic), "steps": steps, "current_step": current}
