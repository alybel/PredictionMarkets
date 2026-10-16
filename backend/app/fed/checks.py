"""Quality checks of the Fed chains: probability sum, missing days, Kalshi vs. Polymarket, daily jumps,
values after the US close. Each check returns the same shape as the data-quality tab's checks."""

import logging
from datetime import date, timedelta

from backend.app.fed.metrics import CHAINS, COMBINED, ChainPoint, Computed, RawPoint, SOURCES, Values, before_decision
from backend.app.fed.parsers import KALSHI, POLYMARKET
from backend.app.quality.pit import is_observable

logger = logging.getLogger(__name__)

PROB_SUM_TOLERANCE = 0.10  # raw sum outside 90–110 %
DIVERGENCE_BP = 10.0       # Kalshi and Polymarket expect more than 10 bp apart
JUMP_BP = 15.0             # a chain moves more than 15 bp in one day
MAX_EXAMPLES = 10


def _check(key: str, label: str, examples: list[dict], detail: str, bad: str = "warn") -> dict:
    return {"key": key, "label": label, "status": bad if examples else "ok", "count": len(examples),
            "detail": detail, "examples": examples[:MAX_EXAMPLES]}


def missing_days(days: set[date]) -> list[date]:
    """Calendar days between the first and the last day that have no value."""
    if not days:
        return []
    first, last = min(days), max(days)
    return [first + timedelta(n) for n in range((last - first).days + 1) if first + timedelta(n) not in days]


def gap_ranges(missing: list[date]) -> list[tuple[date, date]]:
    out: list[list[date]] = []
    for d in missing:
        if out and (d - out[-1][1]).days == 1:
            out[-1][1] = d
        else:
            out.append([d, d])
    return [(a, b) for a, b in out]


def check_prob_sum(values: Values, rejected: Values, tolerance: float = PROB_SUM_TOLERANCE) -> dict:
    rows = [(k, v, False) for k, v in values.items()] + [(k, v, True) for k, v in rejected.items()]
    bad = [{"quelle": s, "sitzung": m.isoformat(), "tag": d.isoformat(), "summe": round(v.raw_sum, 3), "ausgänge": v.outcomes,
            "verworfen": dropped}
           for (s, m, d), v, dropped in sorted(rows, key=lambda r: (r[0][2], r[0][0]), reverse=True)
           if s in SOURCES and v.raw_sum is not None and abs(v.raw_sum - 1.0) > tolerance]
    return _check("prob_sum", "Wahrscheinlichkeitssumme ≠ 100 %", bad,
                  f"Summe der Ausgangspreise einer Sitzung außerhalb {100 - tolerance * 100:.0f}–{100 + tolerance * 100:.0f} %; "
                  "der Wert wird auf 100 % normiert. Unter 50 % oder mit nur einem Ausgang wird der Tag verworfen (Lücke).")


def check_missing(values: Values) -> dict:
    per: dict[tuple[str, date], set[date]] = {}
    for (s, m, d) in values:
        if s in SOURCES:
            per.setdefault((s, m), set()).add(d)
    bad = []
    for (s, m), days in sorted(per.items(), key=lambda kv: kv[0][1], reverse=True):
        for a, b in gap_ranges(missing_days(days)):
            bad.append({"quelle": s, "sitzung": m.isoformat(), "von": a.isoformat(), "bis": b.isoformat(), "tage": (b - a).days + 1})
    return _check("missing_days", "Fehlende Tage", bad,
                  "Lücken zwischen erstem und letztem Tag einer Sitzung je Quelle (zu wenige bepreiste Ausgänge zählt als Lücke).")


def check_divergence(values: Values, limit: float = DIVERGENCE_BP) -> dict:
    bad = []
    for (s, m, d), k in sorted(values.items(), key=lambda kv: kv[0][2], reverse=True):
        p = values.get((POLYMARKET, m, d)) if s == KALSHI else None
        if p is not None and abs(k.expected_bp - p.expected_bp) > limit:
            bad.append({"sitzung": m.isoformat(), "tag": d.isoformat(), "kalshi_bp": round(k.expected_bp, 2),
                        "polymarket_bp": round(p.expected_bp, 2), "abstand_bp": round(abs(k.expected_bp - p.expected_bp), 2)})
    return _check("divergence", "Abweichung Kalshi vs. Polymarket", bad,
                  f"Erwartete Zinsänderung beider Quellen für dieselbe Sitzung und denselben Tag liegt mehr als {limit:.0f} bp auseinander.")


def check_jumps(chains: dict[str, dict[str, list[ChainPoint]]], limit: float = JUMP_BP) -> dict:
    bad = [{"reihe": series, "kette": name, "tag": p.day.isoformat(), "sitzung": p.session_date.isoformat(),
            "änderung_bp": round(p.change, 2), "roll": p.roll}
           for series in (*SOURCES, COMBINED) for name in CHAINS
           for p in reversed(chains.get(series, {}).get(name, [])) if p.change is not None and abs(p.change) > limit]
    bad.sort(key=lambda e: e["tag"], reverse=True)
    return _check("jumps", "Tagessprünge", bad,
                  f"Kette ändert sich an einem Tag um mehr als {limit:.0f} bp (innerhalb derselben Sitzung gemessen).")


def check_after_close(points: list[RawPoint]) -> dict:
    bad = [{"quelle": p.source, "markt": p.external_id, "tag": p.day.isoformat(), "beobachtet": p.observed_at.isoformat()}
           for p in points if not is_observable(p.observed_at, p.day)]
    return _check("after_close", "Werte nach Börsenschluss", bad,
                  "Rohwert beobachtet nach 20:00 UTC (US-Börsenschluss) oder an einem anderen Tag – dürfte nie gespeichert sein.",
                  bad="blocked")


def check_after_decision(points: list[RawPoint]) -> dict:
    bad = [{"quelle": p.source, "markt": p.external_id, "sitzung": p.session_date.isoformat(), "beobachtet": p.observed_at.isoformat()}
           for p in points if p.day == p.session_date and not before_decision(p)]
    return _check("after_decision", "Werte nach der Zinsentscheidung", bad,
                  "Rohwert am Sitzungstag nach 18:00 UTC beobachtet; er fließt nicht in die Ketten ein.", bad="info")


def run_checks(data: Computed, chains: dict[str, dict[str, list[ChainPoint]]]) -> list[dict]:
    checks = [check_prob_sum(data.values, data.rejected), check_missing(data.values), check_divergence(data.values),
              check_jumps(chains), check_after_close(data.points), check_after_decision(data.points)]
    logger.info("fed checks: %s", {c["key"]: c["count"] for c in checks})
    return checks
