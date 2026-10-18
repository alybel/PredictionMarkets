"""Daily series of a chain or single-market metric from the stored daily snapshots (the rules really run here).

Same linking as the Fed chains (``backend.app.fed.metrics.build_chain``): per day only the active link counts,
the daily change is measured within that link (on a roll day against the new link's own previous value), so
the roll causes no jump; ``value`` accumulates these changes from the first level on, ``level`` is the active
link's probability (``down`` links enter as 1 - p, ``uncertainty`` as 1 - 2|p - 0.5|). ``volatility`` links take the
sample standard deviation of the last ``VOL_WINDOW`` within-link daily changes of p as value (no roll jump enters;
a shorter window gives no value). Gaps stay empty and nothing is carried past an end date.
Only point-in-time rows enter (``quality.pit``); violating rows are returned for the lookahead check.
"""

import logging
from collections import Counter, defaultdict, deque
from datetime import date
from statistics import stdev

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.eui.models import MarketSnapshot
from backend.app.fed.checks import gap_ranges, missing_days
from backend.app.metrics.catalog import ChainLink, MetricDef, MetricPoint
from backend.app.metrics.chain_rules import DOWN, FRONT_CHAIN, UNCERTAINTY, VOL_WINDOW, VOLATILITY, metric_reading, reading
from backend.app.metrics.liquidity import average_liquidity
from backend.app.quality.pit import snapshot_violation

logger = logging.getLogger(__name__)

MIN_HISTORY_DAYS = 180  # "sufficient history" (same threshold as the metric finder)
MAX_GAP_EXAMPLES = 10

Prices = dict[tuple[str, str], dict[date, tuple[float, float | None]]]  # link key -> day -> (price, 24h turnover)


def ordered(links) -> list[ChainLink]:
    return sorted(links, key=lambda l: (l.roll_on or date.max, l.source, l.external_id))


def link_series(links, rules, prices: Prices) -> list[MetricPoint]:
    """Linked series of the links (oldest day first); see the module doc."""
    chain, front = ordered(links), FRONT_CHAIN in rules

    def quote(link: ChainLink, day: date | None):
        if day is None or (link.roll_on and day > link.roll_on):  # no forward fill after the end date
            return None
        return prices.get(link.key, {}).get(day)

    def oriented(link: ChainLink, price: float) -> float:
        kind = reading(link.direction)
        return 1.0 - price if kind == DOWN else 1.0 - 2.0 * abs(price - 0.5) if kind == UNCERTAINTY else price

    days = sorted({d for l in chain for d in prices.get(l.key, {}) if not l.roll_on or d <= l.roll_on})
    out: list[MetricPoint] = []
    prev_day, prev_link, linked = None, None, None
    window: deque[float] = deque(maxlen=VOL_WINDOW)  # within-link daily changes of p (never across a roll)
    for day in days:
        eligible = [l for l in chain if not l.roll_on or l.roll_on >= day]
        if front:
            active = next((l for l in eligible if quote(l, day)), None)
        else:
            active = eligible[0] if eligible and quote(eligible[0], day) else None
        if active is None:  # gap: never filled
            continue
        price, volume = quote(active, day)
        before = quote(active, prev_day)
        if before:
            window.append(price - before[0])
        if reading(active.direction) == VOLATILITY:
            roll = prev_link is not None and active.key != prev_link.key
            prev_day, prev_link = day, active
            if len(window) < VOL_WINDOW:  # window below minimum length: no value
                continue
            linked = level = stdev(window)
            change = level - out[-1].value if out else None
            out.append(MetricPoint(day, level, active.roll_on or day, change, roll, volume, level=level))
            continue
        level = oriented(active, price)
        change = level - oriented(active, before[0]) if before else None
        linked = level if linked is None else linked + (change or 0.0)
        out.append(MetricPoint(day, linked, active.roll_on or day, change, prev_link is not None and active.key != prev_link.key,
                               volume, level=level))
        prev_day, prev_link = day, active
    logger.info("chain series run, reading(s) %s: %d links, %d days with value", dict(Counter(reading(l.direction) for l in chain)),
                len(chain), len(out))
    return out


def load_prices(session: Session, links, today: date) -> tuple[Prices, list[dict]]:
    """Point-in-time prices of the links plus the violating rows."""
    wanted = {l.key for l in links}
    stmt = select(MarketSnapshot).where(MarketSnapshot.external_id.in_({e for _, e in wanted}), MarketSnapshot.price.is_not(None))
    prices: Prices = defaultdict(dict)
    violations = []
    for row in session.scalars(stmt):
        if row.key not in wanted:
            continue
        reason = snapshot_violation(row.snapshot_date, row.fetched_at, row.exclusion_reason, today)
        if reason:
            violations.append({"quelle": row.source, "markt": row.external_id, "tag": row.snapshot_date.isoformat(), "grund": reason})
        else:
            prices[row.key][row.snapshot_date] = (row.price, row.volume_24h_usd)
    return prices, violations


def chain_points(session: Session, defn: MetricDef, today: date) -> tuple[list[MetricPoint], list[dict]]:
    """(points oldest first, point-in-time violations) of a chain / single-market metric."""
    prices, violations = load_prices(session, defn.links, today)
    points = link_series(defn.links, defn.rules, prices)
    logger.info("chain metric %s computed (reading %s): %d links, rules %s, %d days, %d rolls, %d point-in-time violations", defn.key,
                metric_reading(defn.links), len(defn.links), list(defn.rules), len(points), sum(p.roll for p in points), len(violations))
    return points, violations


def history_summary(points: list[MetricPoint]) -> dict:
    """Trading days, gaps and liquidity of a series, with a warning below ``MIN_HISTORY_DAYS``."""
    days = {p.day for p in points}
    missing = missing_days(days) if days else []
    gaps = [{"from": a.isoformat(), "to": b.isoformat(), "days": (b - a).days + 1} for a, b in gap_ranges(missing)]
    liquidity = average_liquidity(points)
    short = len(days) < MIN_HISTORY_DAYS
    warnings = [f"Nur {len(days)} Handelstage Historie (mindestens {MIN_HISTORY_DAYS} empfohlen)."] if short else []
    if liquidity is None:
        warnings.append("Keine Umsatzdaten: Liquidität unbekannt.")
    return {"trading_days": len(days), "first_date": min(days).isoformat() if days else None,
            "last_date": max(days).isoformat() if days else None, "gap_days": len(missing), "gaps": gaps[-MAX_GAP_EXAMPLES:],
            "rolls": sum(p.roll for p in points), "liquidity_usd": round(liquidity, 2) if liquidity is not None else None,
            "short_history": short, "min_history_days": MIN_HISTORY_DAYS, "warnings": warnings}
