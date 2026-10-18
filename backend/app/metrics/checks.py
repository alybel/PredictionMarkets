"""Automatic checks of a catalog metric. Every check returns the shape of the data-quality checks
(key, label, status, count, detail, examples); status is ``ok`` or ``blocked`` (a failed check blocks the metric).

* lookahead    – no information of day X on another day: every raw price of the meetings the chain points at
                 was observed on its own day before the US close and, on the meeting day, before the decision.
* coverage     – share of missing calendar days between first and last day stays at or below the threshold.
* roll_jumps   – on a roll day the metric moves only by the within-meeting change (linked series only).
* plausibility – Kalshi and Polymarket expect the chain's meeting within the threshold on almost every day.
Market-basket metrics (Index Manufacture) run lookahead (point-in-time snapshot rows) and coverage only;
chain / single-market metrics (agent step 2) additionally run roll_jumps.
"""

from datetime import date

from backend.app.fed.checks import gap_ranges, missing_days
from backend.app.fed.metrics import CHAINS, COMBINED, SOURCES, Computed, before_decision, chain_session
from backend.app.fed.parsers import KALSHI, POLYMARKET
from backend.app.metrics.catalog import MetricDef, MetricPoint
from backend.app.quality.pit import is_observable

OK, FAILED = "ok", "blocked"
MAX_MISSING_SHARE = 0.10   # at most 10 % missing days
PLAUSIBILITY_BP = 10.0     # Kalshi vs. Polymarket further apart than 10 bp counts as divergent day ...
MAX_DIVERGENT_SHARE = 0.05  # ... and more than 5 % divergent days fail the check
ROLL_TOLERANCE = 1e-6
MAX_EXAMPLES = 10


def _result(key: str, label: str, failed: bool, examples: list[dict], detail: str) -> dict:
    return {"key": key, "label": label, "status": FAILED if failed else OK, "count": len(examples),
            "detail": detail, "examples": examples[:MAX_EXAMPLES]}


def check_lookahead(defn: MetricDef, data: Computed, points: list[MetricPoint], today: date) -> dict:
    offset = CHAINS.index(defn.chain)
    sources = SOURCES if defn.series == COMBINED else (defn.series,)
    bad = []
    for p in data.points:
        if p.source not in sources or chain_session(data.sessions, p.day, offset) != p.session_date:
            continue
        reason = ("nach der Zinsentscheidung" if not before_decision(p)
                  else "anderer Tag oder nach Börsenschluss" if not is_observable(p.observed_at, p.day) else None)
        if reason:
            bad.append({"quelle": p.source, "markt": p.external_id, "tag": p.day.isoformat(),
                        "beobachtet": p.observed_at.isoformat(), "grund": reason})
    bad += [{"tag": p.day.isoformat(), "grund": "Zukunftsdatum"} for p in points if p.day > today]
    return _result("lookahead", "Kein Lookahead", bool(bad), bad,
                   "Jeder Rohpreis der Sitzungen, auf die die Kette zeigt, wurde an seinem eigenen Tag vor 20:00 UTC und "
                   "am Sitzungstag vor der Entscheidung (18:00 UTC) beobachtet; kein Wert liegt in der Zukunft.")


def check_coverage(points: list[MetricPoint], max_share: float = MAX_MISSING_SHARE) -> dict:
    label = "Mindestabdeckung"
    if not points:
        return _result("coverage", label, True, [], "Keine Werte vorhanden.")
    days = {p.day for p in points}
    missing = missing_days(days)
    total = (max(days) - min(days)).days + 1
    share = len(missing) / total
    gaps = [{"von": a.isoformat(), "bis": b.isoformat(), "tage": (b - a).days + 1} for a, b in reversed(gap_ranges(missing))]
    return _result("coverage", label, share > max_share, gaps,
                   f"{len(missing)} von {total} Tagen fehlen ({share:.1%}); erlaubt sind höchstens {max_share:.0%}.")


def check_roll_jumps(points: list[MetricPoint]) -> dict:
    bad = []
    for prev, p in zip(points, points[1:]):
        expected = p.change or 0.0
        if p.roll and abs((p.value - prev.value) - expected) > ROLL_TOLERANCE:
            bad.append({"tag": p.day.isoformat(), "sitzung": p.session_date.isoformat(),
                        "sprung_bp": round(p.value - prev.value, 3), "erwartet_bp": round(expected, 3)})
    return _result("roll_jumps", "Keine Roll-Sprünge", bool(bad), list(reversed(bad)),
                   "Am Roll-Tag bewegt sich die Metrik nur um die Tagesänderung innerhalb der neuen Sitzung (verkettete Reihe).")


def check_plausibility(data: Computed, points: list[MetricPoint], limit_bp: float = PLAUSIBILITY_BP,
                       max_share: float = MAX_DIVERGENT_SHARE) -> dict:
    overlap, bad = 0, []
    for p in points:
        k, pm = data.values.get((KALSHI, p.session_date, p.day)), data.values.get((POLYMARKET, p.session_date, p.day))
        if k is None or pm is None:
            continue
        overlap += 1
        if abs(k.expected_bp - pm.expected_bp) > limit_bp:
            bad.append({"tag": p.day.isoformat(), "sitzung": p.session_date.isoformat(), "kalshi_bp": round(k.expected_bp, 2),
                        "polymarket_bp": round(pm.expected_bp, 2), "abstand_bp": round(abs(k.expected_bp - pm.expected_bp), 2)})
    share = len(bad) / overlap if overlap else 0.0
    return _result("plausibility", "Plausibilität Kalshi vs. Polymarket", share > max_share, list(reversed(bad)),
                   f"{len(bad)} von {overlap} Tagen mit beiden Quellen weichen mehr als {limit_bp:.0f} bp ab ({share:.1%}); "
                   f"erlaubt sind höchstens {max_share:.0%}.")


def run_metric_checks(defn: MetricDef, data: Computed, points: list[MetricPoint], today: date) -> list[dict]:
    return [check_lookahead(defn, data, points, today), check_coverage(points), check_roll_jumps(points),
            check_plausibility(data, points)]


def check_snapshot_lookahead(violations: list[dict]) -> dict:
    return _result("lookahead", "Kein Lookahead", bool(violations), violations,
                   "Jeder Snapshot-Preis wurde an seinem eigenen Tag vor 20:00 UTC beobachtet; kein Wert liegt in der Zukunft.")


def run_basket_checks(points: list[MetricPoint], violations: list[dict]) -> list[dict]:
    return [check_snapshot_lookahead(violations), check_coverage(points)]


def run_chain_checks(points: list[MetricPoint], violations: list[dict]) -> list[dict]:
    return run_basket_checks(points, violations) + [check_roll_jumps(points)]
