"""Definitions of the catalog metrics and their daily series.

A metric is a daily series with a fixed definition (name, unit, source, calculation). The first two are the
Fed chains as jump-free linked level (``ChainPoint.linked`` of backend.app.fed.metrics): on a roll day the
level only moves by the new meeting's change against its own previous day, so the switch causes no jump.
Further metrics are market baskets created from Index Manufacture drafts (``CustomMetric``, unit probability).
"""

from dataclasses import dataclass
from datetime import date

from backend.app.fed.metrics import COMBINED, ChainPoint


@dataclass(frozen=True)
class MetricDef:
    key: str
    name: str
    unit: str
    source: str
    calculation: str
    series: str            # Fed series: kalshi, polymarket or combined ("" for a market basket)
    chain: str             # front or second ("" for a market basket)
    field: str = "linked"  # ChainPoint attribute that forms the metric value
    markets: tuple[tuple[str, str], ...] = ()  # (source, external_id) of a market basket

    @property
    def is_basket(self) -> bool:
        return bool(self.markets)


@dataclass(frozen=True)
class MetricPoint:
    day: date
    value: float
    session_date: date
    change: float | None  # within-meeting change of the day (None: no previous-day value of that meeting)
    roll: bool
    liquidity: float | None = None  # 24h turnover (USD) of the metric's markets on that day (None: not published)


_LINKED = ("Erwartete Zinsänderung (Kalshi und Polymarket je Sitzung auf 100 % normiert, Mittel beider Quellen); "
           "Tagesänderungen innerhalb derselben Sitzung aufsummiert, daher kein Sprung am Roll-Tag. Start bei 0 bp.")

METRICS: tuple[MetricDef, ...] = (
    MetricDef("fed_front_linked", "Fed Front-Kette, verkettet (bp)", "bp", "Kalshi + Polymarket (Fed-Rohdaten, kombiniert)",
              "Nächste noch nicht entschiedene Fed-Sitzung. " + _LINKED, COMBINED, "front"),
    MetricDef("fed_second_linked", "Fed Zweit-Kette, verkettet (bp)", "bp", "Kalshi + Polymarket (Fed-Rohdaten, kombiniert)",
              "Übernächste Fed-Sitzung. " + _LINKED, COMBINED, "second"),
)
BY_KEY = {m.key: m for m in METRICS}
UNIT_PROBABILITY = "probability"
BASKET_SOURCE = "Index Manufacture: Markt-Snapshots Polymarket/Kalshi"


def basket_def(row) -> MetricDef:
    """Definition of a ``CustomMetric`` row."""
    calc = "Liquiditätsgewichteter Preis (24h-Umsatz) der Entwurfs-Märkte je Snapshot-Tag. " + (row.calculation or "")
    return MetricDef(row.key, row.name, UNIT_PROBABILITY, BASKET_SOURCE, calc.strip(), "", "",
                     markets=tuple((m["source"], m["external_id"]) for m in row.markets or []))


def metric_points(defn: MetricDef, chains: dict[str, dict[str, list[ChainPoint]]]) -> list[MetricPoint]:
    """Days of the metric that have a value, oldest first."""
    out = []
    for p in chains.get(defn.series, {}).get(defn.chain, []):
        value = getattr(p, defn.field)
        if value is not None:
            out.append(MetricPoint(p.day, value, p.session_date, p.change, p.roll))
    return out
