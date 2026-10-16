"""Inputs of the stock evaluation of a metric index (1015-2231): z-index, raw metric values, stock and market returns.

Timing as for the archive index (see ``correlation``): the value of day T is known before the US close of T and is
paired with the return close(T) → close(next trading day); never across gaps > MAX_GAP_DAYS. The z-index is the
expanding z-score of ``builder.service`` (day T uses only metric values up to T). Point in time: metric days after
today never enter, stock closes pass ``quality.pit`` in ``closes_by_ticker``, market-basket metrics drop violating
snapshots in ``metrics.basket``.

Market = equal-weighted mean of the next-day returns of the top-100 stocks; excess return = stock − market (same
window). Regressors of the multiple regression are the daily changes of each metric in its raw unit: bp as is,
probabilities in percentage points.
"""

import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy.orm import Session

from backend.app.builder.models import MetricIndex
from backend.app.builder.service import index_payload
from backend.app.metrics.service import evaluate_db
from backend.app.stocks.correlation import closes_by_ticker, next_day_returns, paired_days
from backend.app.stocks.stats import mean
from backend.app.stocks.universe import TOP_US_STOCKS

logger = logging.getLogger(__name__)

EXCESS, RAW = "excess", "raw"
TARGETS = (EXCESS, RAW)
UNIT_SCALE = {"probability": 100.0}           # probabilities as percentage points
UNIT_LABEL = {"bp": "bp", "probability": "Pkt."}
ALL_POINTS = 100_000                          # whole standardized history; the window is cut by calendar days

Returns = dict[date, tuple[date, float]]      # {T: (next trading day, return)}


@dataclass
class MetricInputs:
    name: str
    values: dict[date, float]        # z-index per day
    raw: dict[date, list[float]]     # raw value per component (display unit), same order as ``components``
    components: list[dict]           # {key, name, unit, label}
    warnings: list[str]
    future_days: int                 # days after today that were dropped (point-in-time guard)


@dataclass
class Universe:
    closes: dict[str, dict[date, float]]
    returns: dict[str, Returns]
    market: Returns
    market_days: set[date]


def find_index(session: Session, name: str) -> MetricIndex | None:
    return session.query(MetricIndex).filter_by(name=name).first()


def load_inputs(session: Session, row: MetricIndex, days: int, today: date) -> MetricInputs:
    evaluations = evaluate_db(session, today)
    payload = index_payload(session, row, evaluations, ALL_POINTS)
    used = [c for c in payload["components"] if c["status"] != "unknown"]
    raw_by_key = {c["key"]: {p.day: p.value * UNIT_SCALE.get(c["unit"], 1.0) for p in evaluations[c["key"]].points}
                  for c in used}
    cutoff, values, raw, future = today - timedelta(days=days), {}, {}, 0
    for point in payload["points"]:
        day = date.fromisoformat(point["date"])
        if day > today:
            future += 1
            continue
        if day >= cutoff:
            values[day] = point["value"]
            raw[day] = [raw_by_key[c["key"]][day] for c in used]
    if future:
        logger.warning("point-in-time guard: %d future-dated days of metric index %r blocked", future, row.name)
    components = [{"key": c["key"], "name": c["name"], "unit": c["unit"], "label": UNIT_LABEL.get(c["unit"], c["unit"])}
                  for c in used]
    return MetricInputs(row.name, values, raw, components, payload["warnings"], future)


def market_returns(returns: dict[str, Returns]) -> Returns:
    """Equal-weighted mean return per day; the exit day is the one most stocks share (all share it without data holes)."""
    groups: dict[tuple[date, date], list[float]] = defaultdict(list)
    for rets in returns.values():
        for day, (nxt, ret) in rets.items():
            groups[(day, nxt)].append(ret)
    out: Returns = {}
    for (day, nxt), rets in sorted(groups.items(), key=lambda kv: len(kv[1])):  # the largest group wins
        out[day] = (nxt, mean(rets))
    return out


def excess_returns(rets: Returns, market: Returns) -> Returns:
    """Stock minus market over the same window (days where the windows differ are left out)."""
    return {day: (nxt, ret - market[day][1]) for day, (nxt, ret) in rets.items() if market.get(day, (None,))[0] == nxt}


def load_universe(session: Session, start: date | None) -> Universe:
    closes = {t: c for t, c in closes_by_ticker(session, start).items() if t in TOP_US_STOCKS} if start else {}
    returns = {t: next_day_returns(c) for t, c in closes.items()}
    return Universe(closes, returns, market_returns(returns), set().union(*closes.values()) if closes else set())


def target_returns(universe: Universe, ticker: str, target: str) -> tuple[set[date], Returns]:
    """(trading days, returns) of a stock; ``target`` excess subtracts the market."""
    rets = universe.returns.get(ticker, {})
    return set(universe.closes.get(ticker, {})), excess_returns(rets, universe.market) if target == EXCESS else rets


def observations(inputs: MetricInputs, trading_days: set[date], rets: Returns) -> list[dict]:
    """One row per pair: T, exit day, Δz of the index, Δ raw value per component, target return."""
    out = []
    for prev, day, nxt, ret in paired_days(inputs.values, trading_days, rets):
        out.append({"day": day, "exit": nxt, "dz": inputs.values[day] - inputs.values[prev],
                    "dx": [b - a for a, b in zip(inputs.raw[prev], inputs.raw[day])], "ret": ret})
    return out
