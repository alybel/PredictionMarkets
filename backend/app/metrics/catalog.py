"""Definitions of the catalog metrics and their daily series.

A metric is a daily series with a fixed definition (name, unit, source, calculation). The first two are the
Fed chains as jump-free linked level (``ChainPoint.linked`` of backend.app.fed.metrics): on a roll day the
level only moves by the new meeting's change against its own previous day, so the switch causes no jump.
Further metrics are market baskets created from Index Manufacture drafts (``CustomMetric``, unit probability) and
structured chain / single-market metrics from the agent (``CustomMetric.kind`` chain or single, computed by ``metrics.chain``).
"""

from dataclasses import dataclass
from datetime import date

from backend.app.fed.metrics import COMBINED, ChainPoint
from backend.app.metrics.chain_rules import READINGS, RULES, metric_reading, reading


@dataclass(frozen=True)
class ChainLink:
    """One link of a chain metric: active up to and including ``roll_on`` (None: no end date); ``direction`` is its reading
    (``chain_rules.READINGS``: up = p, down = 1 - p, uncertainty, volatility)."""

    source: str
    external_id: str
    title: str
    roll_on: date | None
    direction: str = "up"

    @property
    def key(self) -> tuple[str, str]:
        return (self.source, self.external_id)


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
    links: tuple[ChainLink, ...] = ()           # links of a chain / single-market metric
    rules: tuple[str, ...] = ()                 # executed rule keys (metrics.chain_rules)
    notes: tuple[str, ...] = ()                 # free-text rules, shown only, never executed

    @property
    def is_basket(self) -> bool:
        return bool(self.markets)

    @property
    def is_chain(self) -> bool:
        return bool(self.links)


@dataclass(frozen=True)
class MetricPoint:
    day: date
    value: float
    session_date: date
    change: float | None  # within-meeting change of the day (None: no previous-day value of that meeting)
    roll: bool
    liquidity: float | None = None  # 24h turnover (USD) of the metric's markets on that day (None: not published)
    level: float | None = None      # chain metrics: probability level of the active link (direction applied)


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
CHAIN_SOURCE = "Domänen-Rohdaten: Markt-Snapshots Polymarket/Kalshi"
CHAIN_KINDS = ("chain", "single")


def basket_def(row) -> MetricDef:
    """Definition of a ``CustomMetric`` row."""
    calc = "Liquiditätsgewichteter Preis (24h-Umsatz) der Entwurfs-Märkte je Snapshot-Tag. " + (row.calculation or "")
    return MetricDef(row.key, row.name, UNIT_PROBABILITY, BASKET_SOURCE, calc.strip(), "", "",
                     markets=tuple((m["source"], m["external_id"]) for m in row.markets or []))


def chain_def(row) -> MetricDef:
    """Definition of a structured ``CustomMetric`` row (kind chain or single)."""
    spec = row.spec or {}
    links = tuple(ChainLink(l["source"], l["external_id"], l.get("title") or "", date.fromisoformat(l["roll_on"]) if l.get("roll_on") else None,
                            reading(l.get("direction"))) for l in spec.get("links") or [])
    rules, notes = tuple(k for k in spec.get("rules") or [] if k in RULES), tuple(spec.get("notes") or [])
    head = (f"Kette aus {len(links)} Gliedern: je Tag nur das aktive Glied, Tagesänderung nur innerhalb eines Glieds, verkettete Reihe "
            "ab dem ersten Niveau." if row.kind == "chain" else "Einzelmarkt: Wahrscheinlichkeit je Snapshot-Tag.")
    readings = metric_reading(links).split(", ") if links else []
    parts = [head, "Lesart: " + "; ".join(f"{READINGS[r]['label']} – {READINGS[r]['text']}" for r in readings) if readings else "",
             "Regeln: " + "; ".join(RULES[k]["label"] for k in rules) + "."]
    if notes:
        parts.append("Hinweise, nicht automatisch umgesetzt: " + "; ".join(notes))
    parts.append(row.calculation or "")
    return MetricDef(row.key, row.name, UNIT_PROBABILITY, CHAIN_SOURCE, " ".join(p for p in parts if p).strip()[:4000], "", "",
                     links=links, rules=rules, notes=notes)


def metric_points(defn: MetricDef, chains: dict[str, dict[str, list[ChainPoint]]]) -> list[MetricPoint]:
    """Days of the metric that have a value, oldest first."""
    out = []
    for p in chains.get(defn.series, {}).get(defn.chain, []):
        value = getattr(p, defn.field)
        if value is not None:
            out.append(MetricPoint(p.day, value, p.session_date, p.change, p.roll))
    return out
