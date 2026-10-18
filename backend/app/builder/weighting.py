"""Weighting methods of a metric index (risk index 5/7).

* ``manual``: the stored weights, constant over time (unchanged behaviour of every existing index).
* ``equal``: 1/n per metric.
* ``liquidity``: proportional to the metric's average daily turnover over its last ``LIQ_WINDOW`` days with data.
* ``inverse_vol``: proportional to 1 / standard deviation of the metric's daily z-score changes over the last
  ``VOL_WINDOW`` changes (z-scores make bp and probabilities comparable).

The weights of day t use only data up to t (no look-ahead) and always sum to 1. When a data-driven method lacks data
for a metric on a day, that day falls back to equal weights (counted in ``fallback_days``).
"""

from bisect import bisect_right
from datetime import date
from statistics import pstdev

from backend.app.metrics.catalog import MetricPoint
from backend.app.metrics.liquidity import average_liquidity

MANUAL, EQUAL, LIQUIDITY, INVERSE_VOL = "manual", "equal", "liquidity", "inverse_vol"
METHODS = {MANUAL: "Manuell", EQUAL: "Gleichgewichtet", LIQUIDITY: "Nach Liquidität", INVERSE_VOL: "Inverse Volatilität"}
LIQ_WINDOW = 30
VOL_WINDOW = 60
MIN_CHANGES = 5


def normalize(raw: dict[str, float]) -> dict[str, float]:
    total = sum(raw.values())
    return {k: v / total for k, v in raw.items()}


def percent(weights: dict[str, float]) -> dict[str, float]:
    """Weights in percent with one decimal that add up to exactly 100 (largest remainder)."""
    if not weights:
        return {}
    tenths = {k: w * 1000 for k, w in normalize(weights).items()}
    out = {k: int(v) for k, v in tenths.items()}
    for k in sorted(tenths, key=lambda k: tenths[k] - out[k], reverse=True)[:1000 - sum(out.values())]:
        out[k] += 1
    return {k: v / 10 for k, v in out.items()}


class Weighting:
    """Point-in-time weights of one method for the components ``keys``."""

    def __init__(self, method: str, points: dict[str, list[MetricPoint]], z_by_key: dict[str, dict],
                 manual: dict[str, float] | None = None):
        if method not in METHODS:
            raise ValueError(f"unknown weighting method {method!r}")
        self.method, self.keys, self.manual, self.fallback_days = method, list(z_by_key), manual or {}, 0
        self._points = {k: (points[k], [p.day for p in points[k]]) for k in self.keys}
        self._changes = {}
        for k, z in z_by_key.items():
            days = sorted(z)
            self._changes[k] = (days, [0.0] + [z[b] - z[a] for a, b in zip(days, days[1:])])

    def liquidity(self, key: str, day: date) -> float | None:
        points, days = self._points[key]
        return average_liquidity(points[:bisect_right(days, day)], LIQ_WINDOW)

    def volatility(self, key: str, day: date) -> float | None:
        days, changes = self._changes[key]
        idx = bisect_right(days, day)
        window = changes[max(1, idx - VOL_WINDOW):idx]
        return pstdev(window) if len(window) >= MIN_CHANGES else None

    def raw(self, day: date) -> dict[str, float] | None:
        """Unnormalized weights of the method on ``day``; None when a metric lacks the data."""
        if self.method == MANUAL:
            return {k: self.manual.get(k, 1.0) for k in self.keys}
        if self.method == EQUAL:
            return {k: 1.0 for k in self.keys}
        if self.method == LIQUIDITY:
            raw = {k: self.liquidity(k, day) for k in self.keys}
            return raw if all(v is not None and v > 0 for v in raw.values()) else None
        vols = {k: self.volatility(k, day) for k in self.keys}
        return {k: 1 / v for k, v in vols.items()} if all(v is not None and v > 0 for v in vols.values()) else None

    def at(self, day: date) -> dict[str, float]:
        """Normalized weights of ``day`` (sum 1); equal weights when the method lacks data."""
        if not self.keys:
            return {}
        raw = self.raw(day)
        if raw is None:
            self.fallback_days += 1
            raw = {k: 1.0 for k in self.keys}
        return normalize(raw)


def weighted_series(z_by_key: dict[str, dict], weigh) -> list[dict]:
    """Index value per day all components have a z-score: sum of weight(day) * z, weights from ``weigh(day)``."""
    if not z_by_key:
        return []
    days = sorted(set.intersection(*(set(z) for z in z_by_key.values())))
    out = []
    for d in days:
        w = weigh(d)
        out.append({"date": d.isoformat(), "value": round(sum(w[k] * z[d] for k, z in z_by_key.items()), 4),
                    "components": {k: round(z[d], 4) for k, z in z_by_key.items()}})
    return out
