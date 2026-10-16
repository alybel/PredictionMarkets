"""Derived data layer of the Fed chains, computed from the raw layer on every call (nothing stored twice).

* Per meeting, day and source the outcome prices are normalized to 100 %; the expected rate change is
  sum(probability x bp). The raw price sum before normalization is kept as quality figure.
* A value needs at least two priced outcomes and a raw sum of at least 50 % — a missing main outcome would
  otherwise turn the normalization upside down; such days are kept as ``rejected`` for the checks.
* On the meeting day only prices observed before the decision (18:00 UTC) count.
* ``combined`` is the mean of Kalshi and Polymarket where both exist, else the one available.
* Front chain = next meeting not yet decided (the meeting day itself still counts: every stored price of
  that day was observed before the decision); second chain = the meeting after it. The day after a
  decision both chains roll one meeting on.
* Chain linking: the daily change of a chain is always measured within one meeting — on a roll day it is
  the new meeting's change against its own previous-day value, so the switch causes no jump.
  ``linked`` accumulates these changes into a jump-free level.
"""

import bisect
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.fed.models import FedMarket, FedPrice
from backend.app.fed.parsers import KALSHI, POLYMARKET
from backend.app.quality.pit import as_utc, at_utc

logger = logging.getLogger(__name__)

COMBINED = "combined"
SOURCES = (KALSHI, POLYMARKET)
SERIES = (KALSHI, POLYMARKET, COMBINED)
CHAINS = ("front", "second")
MIN_OUTCOMES = 2
MIN_RAW_SUM = 0.5
DECISION_UTC = time(18)  # FOMC statements: 14:00 New York = 18:00 UTC in summer, 19:00 UTC in winter


@dataclass(frozen=True)
class RawPoint:
    source: str
    session_date: date
    outcome_bp: int
    outcome_label: str
    external_id: str
    day: date
    price: float
    volume_usd: float | None
    observed_at: datetime
    origin: str


@dataclass
class SessionValue:
    expected_bp: float
    raw_sum: float | None
    outcomes: int
    sources: list[str] = field(default_factory=list)


Values = dict[tuple[str, date, date], SessionValue]  # (series, meeting, day) -> value


@dataclass(frozen=True)
class ChainPoint:
    day: date
    session_date: date
    value: float | None
    change: float | None
    roll: bool
    linked: float | None


def expected_change(outcomes: list[tuple[float, int]]) -> tuple[float, float] | None:
    """(expected change in bp, raw probability sum) of (price, bp) pairs; None if nothing is priced."""
    raw = sum(p for p, _ in outcomes)
    if raw <= 0:
        return None
    return sum(p / raw * bp for p, bp in outcomes), raw


def raw_points(session: Session) -> list[RawPoint]:
    rows = session.execute(select(FedMarket.source, FedMarket.session_date, FedMarket.outcome_bp, FedMarket.outcome_label,
                                  FedMarket.external_id, FedPrice.price_date, FedPrice.price, FedPrice.volume_usd,
                                  FedPrice.observed_at, FedPrice.origin).join(FedPrice, FedPrice.market_id == FedMarket.id))
    return [RawPoint(*r[:8], as_utc(r[8]), r[9]) for r in rows]  # SQLite returns naive UTC


def before_decision(p: RawPoint) -> bool:
    """Observed before the meeting's decision: any earlier day, or the meeting day before 18:00 UTC."""
    return p.day < p.session_date or (p.day == p.session_date and p.observed_at < at_utc(p.day, DECISION_UTC))


def session_values(points: list[RawPoint]) -> tuple[Values, Values]:
    """(accepted, rejected) values per (source, meeting, day); rejected: too few outcomes or raw sum below 50 %."""
    grouped: dict[tuple[str, date, date], list[tuple[float, int]]] = {}
    for p in points:
        if before_decision(p):
            grouped.setdefault((p.source, p.session_date, p.day), []).append((p.price, p.outcome_bp))
    accepted, rejected = {}, {}
    for key, outcomes in grouped.items():
        result = expected_change(outcomes)
        value = SessionValue(result[0] if result else 0.0, result[1] if result else 0.0, len(outcomes), [key[0]])
        ok = result is not None and len(outcomes) >= MIN_OUTCOMES and result[1] >= MIN_RAW_SUM
        (accepted if ok else rejected)[key] = value
    return accepted, rejected


def combine(values: Values) -> Values:
    """Adds the ``combined`` series: mean of both sources where both exist, else the one available."""
    by_key: dict[tuple[date, date], list[SessionValue]] = {}
    for (source, session_date, day), v in values.items():
        if source in SOURCES:
            by_key.setdefault((session_date, day), []).append(v)
    out = dict(values)
    for (session_date, day), vs in by_key.items():
        sums = [v.raw_sum for v in vs if v.raw_sum is not None]
        out[(COMBINED, session_date, day)] = SessionValue(sum(v.expected_bp for v in vs) / len(vs),
                                                          sum(sums) / len(sums) if sums else None,
                                                          sum(v.outcomes for v in vs), sorted(s for v in vs for s in v.sources))
    return out


def chain_session(sessions: list[date], day: date, offset: int) -> date | None:
    """Meeting the chain points at on ``day``: offset 0 = front (first meeting on or after the day), 1 = second."""
    idx = bisect.bisect_left(sessions, day) + offset
    return sessions[idx] if idx < len(sessions) else None


def build_chain(values: Values, series: str, sessions: list[date],
                offset: int) -> list[ChainPoint]:
    days = sorted({d for (s, _, d) in values if s == series})
    out: list[ChainPoint] = []
    prev_day: date | None = None
    prev_session: date | None = None
    linked = 0.0
    for day in days:
        session_date = chain_session(sessions, day, offset)
        if session_date is None:
            continue
        current = values.get((series, session_date, day))
        before = values.get((series, session_date, prev_day)) if prev_day else None
        change = current.expected_bp - before.expected_bp if current and before else None
        roll = prev_session is not None and session_date != prev_session
        linked = linked + change if change is not None else linked
        out.append(ChainPoint(day, session_date, current.expected_bp if current else None, change, roll,
                              linked if current else None))
        prev_day, prev_session = day, session_date
    return out


def all_sessions(points: list[RawPoint]) -> list[date]:
    return sorted({p.session_date for p in points})


def build_chains(values: Values, sessions: list[date]) -> dict[str, dict[str, list[ChainPoint]]]:
    out: dict[str, dict[str, list[ChainPoint]]] = {}
    for series in SERIES:
        out[series] = {}
        for offset, name in enumerate(CHAINS):
            chain = build_chain(values, series, sessions, offset)
            out[series][name] = chain
            rolls = [f"{p.day}->{p.session_date}" for p in chain if p.roll]
            logger.info("fed chain %s/%s: %d days, %d with value, %d rolls %s", series, name, len(chain),
                        sum(p.value is not None for p in chain), len(rolls), rolls)
    return out


@dataclass
class Computed:
    points: list[RawPoint]
    values: Values      # accepted values incl. the combined series
    rejected: Values    # incomplete days, kept for the checks
    sessions: list[date]


def compute(session: Session) -> Computed:
    """Raw points, session values (incl. combined), rejected days and meeting dates of the whole raw layer."""
    points = raw_points(session)
    accepted, rejected = session_values(points)
    result = Computed(points, combine(accepted), rejected, all_sessions(points))
    logger.info("fed compute: %d raw points, %d meetings, %d session values, %d rejected days", len(points),
                len(result.sessions), len(result.values), len(rejected))
    return result
