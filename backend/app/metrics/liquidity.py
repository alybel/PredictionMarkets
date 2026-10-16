"""Liquidity of the catalog metrics: daily 24h turnover (USD) of the markets behind a metric and its recent average.

* Fed chains: summed daily volume of every outcome market of the meeting the chain points at, for the chain's
  sources, observed before the decision (Polymarket publishes no daily volume, so it adds nothing).
* Market baskets: summed 24h turnover of the basket's markets (set in ``basket.basket_points``).
* ``average_liquidity``: mean over the last ``WINDOW_DAYS`` days that carry a liquidity; this figure filters metrics.
* ``median_liquidity``: median of those current averages across metrics, the reference point for the filter (1016-1007).
  The filter acts on the current edge only: it hides metrics, their history stays complete.
"""

from collections import defaultdict
from dataclasses import replace
from datetime import date
from statistics import median

from backend.app.fed.metrics import COMBINED, SOURCES, Computed, before_decision
from backend.app.metrics.catalog import MetricDef, MetricPoint

WINDOW_DAYS = 30


def fed_volumes(data: Computed) -> dict[tuple[str, date, date], float]:
    """(source, meeting, day) -> summed daily volume in USD of the raw points that publish one."""
    out: dict[tuple[str, date, date], float] = defaultdict(float)
    for p in data.points:
        if p.volume_usd is not None and before_decision(p):
            out[(p.source, p.session_date, p.day)] += p.volume_usd
    return out


def with_chain_liquidity(defn: MetricDef, volumes: dict, points: list[MetricPoint]) -> list[MetricPoint]:
    sources = SOURCES if defn.series == COMBINED else (defn.series,)
    out = []
    for p in points:
        found = [volumes[k] for k in ((s, p.session_date, p.day) for s in sources) if k in volumes]
        out.append(replace(p, liquidity=sum(found) if found else None))
    return out


def average_liquidity(points: list[MetricPoint], window: int = WINDOW_DAYS) -> float | None:
    values = [p.liquidity for p in points[-window:] if p.liquidity is not None]
    return sum(values) / len(values) if values else None


def passes(liquidity: float | None, min_liquidity: float | None) -> bool:
    """Liquidity filter: without a minimum everything passes; with one, unknown liquidity fails."""
    return not min_liquidity or (liquidity is not None and liquidity >= min_liquidity)


def median_liquidity(values) -> dict:
    """Median of the current (30-day average) liquidity; metrics without turnover data are left out but counted."""
    values = list(values)
    known = [v for v in values if v is not None]
    return {"usd": round(median(known), 2) if known else None, "metrics": len(known), "of": len(values)}
