"""Forecast report of a metric index (risk index 6/7): S&P 500, 10-year yield, TLT and VIX over 1/5/20 trading days
plus crash hit rate, each measured walk-forward (``walkforward``, ``crash``), with one verdict per target.

Verdict per target: checks = 3 horizons (+ crash for price series); a horizon passes if its one-sided p is below the
Bonferroni level ALPHA / checks and the out-of-sample R² is positive, the crash check if p is below it and the lift
above 1. Score = share of passed checks in percent; light: green ≥ 50, yellow > 0, red 0, grey = nothing measurable.

Point in time: index values come from ``load_inputs`` (expanding z-score, nothing after ``as_of``), closes from
``closes_by_ticker`` (``quality.pit``) cut at ``as_of``. The report is stored per index on every save and daily.
"""

import logging
from datetime import date

from sqlalchemy.orm import Session

from backend.app.builder.models import MetricIndex
from backend.app.builder.weighting import MANUAL
from backend.app.forecast.crash import crash_observations, evaluate_crash
from backend.app.forecast.models import ForecastReport
from backend.app.forecast.targets import TARGETS, TICKERS, Target
from backend.app.forecast.walkforward import MIN_EFFECTIVE, evaluate, observations
from backend.app.history import MAX_DAYS
from backend.app.models import utcnow
from backend.app.stocks.correlation import ALPHA, closes_by_ticker
from backend.app.stocks.metric_data import load_inputs
from backend.app.stocks.prices import refresh_prices

logger = logging.getLogger(__name__)

HORIZONS = (1, 5, 20)
MIN_HISTORY_DAYS = 3 * 365
LIGHTS = {"green": "Grün – belastbare Prognosekraft", "yellow": "Gelb – einzelne Signale",
          "red": "Rot – keine belastbare Prognosekraft", "grey": "Grau – zu wenig Daten"}


def de(value: float | None, digits: int = 2) -> str:
    return "–" if value is None else f"{value:.{digits}f}".replace(".", ",")


def de_p(p: float | None) -> str:
    return "< 0,0001" if p is not None and p < 1e-4 else "= " + de(p, 4)


def _passes(check: dict | None, alpha: float, effect: str, floor: float) -> bool:
    return bool(check and check["enough"] and check["p_value"] is not None and check["p_value"] < alpha
                and check[effect] is not None and check[effect] > floor)


def _explain(target: Target, horizons: list[dict], crash: dict | None, closes: int, verdict: dict) -> str:
    if not closes:
        return f"Keine Kurse für {target.name} gespeichert – der tägliche Lauf oder „Kurse laden“ holt sie."
    if verdict["light"] == "grey":
        return (f"Zu wenig gemeinsame Tage von Index und {target.name}: je Zeitraum sind mindestens {MIN_EFFECTIVE} "
                "unabhängige Testbeobachtungen außerhalb der Lernphase nötig.")
    text = (f"{verdict['passed']} von {verdict['checks']} Prüfungen außerhalb der Lernphase bestanden "
            f"(Signifikanz nach Bonferroni: p < {de(verdict['alpha'], 4)}).")
    measured = [h for h in horizons if h["forecast_correlation"] is not None]
    if measured:
        best = max(measured, key=lambda h: h["forecast_correlation"])
        text += (f" Am stärksten über {best['horizon']} Handelstag{'e' if best['horizon'] > 1 else ''}: Prognose-Korrelation "
                 f"{de(best['forecast_correlation'])}, p {de_p(best['p_value'])}, Out-of-Sample-R² {de(100 * (best['oos_r2'] or 0), 1)} %.")
    if crash and crash["enough"]:
        text += (f" Crash-Signal: {de(100 * crash['hit_rate'], 0)} % Treffer bei {de(100 * crash['base_rate'], 0)} % "
                 f"Grundrate (Lift {de(crash['lift'])}).")
    return text + {"green": f" Der Index hatte belastbare Vorlauf-Signale für {target.name}.",
                   "yellow": " Einzelne belastbare Signale, aber nicht über mehrere Zeiträume.",
                   "red": " Keine belastbare Prognosekraft: was in der Lernphase galt, hielt außerhalb nicht."}[verdict["light"]]


def target_report(target: Target, values: dict[date, float], closes: dict[date, float]) -> dict:
    horizons = [evaluate(observations(values, closes, h, target.kind, target.scale), h) for h in HORIZONS]
    crash = evaluate_crash(crash_observations(values, closes)) if target.crash else None
    checks = len(horizons) + (crash is not None)
    alpha = ALPHA / checks
    for h in horizons:
        h["passed"] = _passes(h, alpha, "oos_r2", 0.0)
    if crash is not None:
        crash["passed"] = _passes(crash, alpha, "lift", 1.0)
    passed = sum(h["passed"] for h in horizons) + bool(crash and crash["passed"])
    measured = sum(h["enough"] for h in horizons) + bool(crash and crash["enough"])
    score = round(100 * passed / checks)
    light = "grey" if not measured else "green" if score >= 50 else "yellow" if passed else "red"
    verdict = {"score": score, "passed": passed, "checks": checks, "measured": measured, "alpha": alpha,
               "light": light, "light_label": LIGHTS[light]}
    return {"ticker": target.ticker, "name": target.name, "kind": target.kind, "unit": target.unit, "closes": len(closes),
            "horizons": horizons, "crash": crash, **verdict,
            "explanation": _explain(target, horizons, crash, len(closes), verdict)}


def build_report(name: str, values: dict[date, float], closes: dict[str, dict[date, float]], as_of: date,
                 weighting: str = MANUAL) -> dict:
    """Pure report from index values and target closes (both already cut at ``as_of``)."""
    days = sorted(values)
    span = (days[-1] - days[0]).days if days else 0
    warnings = []
    if span < MIN_HISTORY_DAYS:
        warnings.append(f"Historie nur {span // 30} Monate (unter 3 Jahren): Ergebnisse sind wenig belastbar, "
                        "weil kaum Marktphasen und Crashs enthalten sind.")
    if weighting == MANUAL:
        warnings.append("Gewichte manuell festgelegt: Die Prüfung lernt Richtung und Schwellen nur aus Daten vor jedem "
                        "Testblock, die manuelle Wahl der Gewichte selbst kann aber Wissen aus der gesamten Historie enthalten.")
    targets = [target_report(t, values, closes.get(t.ticker, {})) for t in TARGETS]
    for t in targets:
        for h in t["horizons"]:
            logger.info("forecast run: index=%r target=%s horizon=%d index_days=%d test_n=%d forecast_r=%s p=%s oos_r2=%s passed=%s",
                        name, t["ticker"], h["horizon"], len(values), h["test_n"], h["forecast_correlation"], h["p_value"],
                        h["oos_r2"], h["passed"])
        if t["crash"]:
            c = t["crash"]
            logger.info("forecast run: index=%r target=%s crash test_days=%d crash_days=%d signals=%d hit_rate=%s lift=%s p=%s",
                        name, t["ticker"], c["test_days"], c["crash_days"], c["signals"], c["hit_rate"], c["lift"], c["p_value"])
        logger.info("forecast verdict: index=%r target=%s score=%d light=%s (%d/%d checks)", name, t["ticker"], t["score"],
                    t["light"], t["passed"], t["checks"])
    return {"index_name": name, "as_of": as_of.isoformat(), "index_days": len(values),
            "first_date": days[0].isoformat() if days else None, "last_date": days[-1].isoformat() if days else None,
            "history_years": round(span / 365.25, 2), "short_history": span < MIN_HISTORY_DAYS, "warnings": warnings,
            "weighting": weighting, "horizons": list(HORIZONS), "alpha": ALPHA, "targets": targets}


def compute_report(session: Session, row: MetricIndex, as_of: date | None = None) -> dict:
    as_of = as_of or utcnow().date()
    values = load_inputs(session, row, MAX_DAYS, as_of).values
    start = min(values) if values else None
    closes = {t: {d: c for d, c in closes_by_ticker(session, start, only=t).get(t, {}).items() if d <= as_of}
              for t in TICKERS} if start else {}
    return build_report(row.name, values, closes, as_of, row.weighting or MANUAL)


def store_report(session: Session, row: MetricIndex, trigger: str, as_of: date | None = None) -> dict:
    payload = compute_report(session, row, as_of)
    report = session.query(ForecastReport).filter_by(index_name=row.name).first() or ForecastReport(index_name=row.name)
    report.as_of, report.trigger, report.computed_at = date.fromisoformat(payload["as_of"]), trigger, utcnow()
    report.payload = {**payload, "trigger": trigger, "computed_at": report.computed_at.isoformat()}
    session.add(report)
    session.commit()
    logger.info("forecast report of %r stored (trigger %s, as of %s, %d index days, lights %s)", row.name, trigger,
                payload["as_of"], payload["index_days"], {t["ticker"]: t["light"] for t in payload["targets"]})
    return report.payload


def refresh_report(session: Session, row: MetricIndex, trigger: str) -> dict | None:
    """``store_report`` that never raises: a failing report must not fail the save or the daily run."""
    try:
        return store_report(session, row, trigger)
    except Exception:
        session.rollback()
        logger.exception("forecast report of %r failed (trigger %s)", row.name, trigger)
        return None


def stored_report(session: Session, name: str) -> dict | None:
    report = session.query(ForecastReport).filter_by(index_name=name).first()
    return report.payload if report else None


def delete_report(session: Session, name: str) -> None:
    session.query(ForecastReport).filter_by(index_name=name).delete()


def refresh_all(session: Session, trigger: str = "daily") -> dict:
    rows = session.query(MetricIndex).order_by(MetricIndex.name).all()
    done = [r.name for r in rows if refresh_report(session, r, trigger) is not None]
    logger.info("forecast reports refreshed (%s): %d of %d indices %s", trigger, len(done), len(rows), done)
    return {"indices": len(rows), "reports": len(done)}


def refresh_benchmarks(session: Session, fetcher=None) -> dict:
    """Closes of the four targets, back to the full history window (one request per ticker; None: the raw data layer)."""
    return refresh_prices(session, fetcher, tickers=TICKERS, backfill=True, trigger="forecast")


def daily_forecast(session: Session, fetcher=None) -> dict:
    """Daily step: load the target closes (a failure keeps the stored ones), then rebuild every report."""
    try:
        prices = refresh_benchmarks(session, fetcher)
    except Exception:
        session.rollback()
        logger.exception("forecast target prices failed; reports use the stored closes")
        prices = None
    return {"prices": prices, **refresh_all(session, "daily")}
