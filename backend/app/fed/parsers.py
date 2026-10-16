"""Pure parsers of the Fed decision markets of Kalshi and Polymarket (no network).

* Kalshi: events of the series ``KXFEDDECISION`` (older ones are named ``FEDDECISION-…``). The event's
  ``strike_date`` is the decision time, its UTC date the meeting day. Each market is one outcome, given as
  ``custom_strike`` {"Cut"|"Hike": "25" | ">25" | "0"}; ">25" counts as 50 bp. Daily candles end at
  midnight New York; their close is the day's price, contracts x price the USD turnover. Live candles
  quote ``close_dollars``/``volume_fp``, the historical API ``close``/``volume``.
* Polymarket: events titled "Fed decision in …" / "Fed Interest Rates: …"; each market is one outcome,
  named in ``groupItemTitle`` ("50+ bps decrease", "No change", "25 bps increase"); "Other" is skipped.
  ``prices-history`` gives the Yes price per day; no daily volume is published.

Point in time (``quality.pit``): a point counts only for the UTC day it was observed on, before that day's
US close, and never after ``now`` (an unfinished candle).
"""

import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from backend.app.clients.base import parse_float
from backend.app.quality.pit import as_utc, is_observable

KALSHI = "kalshi"
POLYMARKET = "polymarket"
KALSHI_WEB_URL = "https://kalshi.com/markets/{ticker}"
POLYMARKET_WEB_URL = "https://polymarket.com/event/{slug}"
ABOVE_STEP_BP = 50  # "Cut >25bps" / "Hike >25bps": the next 25 bp step
SNAP_DAYS = 3       # a Polymarket end date within this many days of a known meeting belongs to it
POLY_TITLE = re.compile(r"^\s*fed (decision in|interest rates)", re.I)
POLY_OUTCOME = re.compile(r"(\d+)\s*\+?\s*bps?\s*(decrease|cut|increase|hike)", re.I)


@dataclass(frozen=True)
class FedMarketInfo:
    source: str
    external_id: str
    event_id: str
    session_date: date
    outcome_bp: int
    outcome_label: str
    title: str
    url: str | None
    history_key: str  # Kalshi ticker or Polymarket Yes token
    opened_at: datetime | None = None


@dataclass(frozen=True)
class FedPoint:
    day: date
    price: float
    volume_usd: float | None
    observed_at: datetime


def _parse_time(value) -> datetime | None:
    if not value:
        return None
    try:
        return as_utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))
    except ValueError:
        return None


def _keep(points: dict[date, FedPoint], observed: datetime, price: float | None, volume: float | None,
          now: datetime) -> None:
    """Add a point if it is observable on its UTC day; the latest point of a day wins."""
    if price is None or not 0.0 <= price <= 1.0 or observed > now or not is_observable(observed, observed.date()):
        return
    day = observed.date()
    if day not in points or points[day].observed_at <= observed:
        points[day] = FedPoint(day, price, volume, observed)


# ---- Kalshi ---------------------------------------------------------------------------------

def kalshi_outcome_bp(market: dict) -> int | None:
    """Rate step of a Kalshi outcome in bp: Cut 25 -> -25, Cut >25 -> -50, Hike 0 -> 0, Hike >25 -> +50."""
    strike = market.get("custom_strike")
    if isinstance(strike, dict) and len(strike) == 1:
        (direction, size), = strike.items()
        sign = {"cut": -1, "hike": 1}.get(str(direction).strip().lower())
        size = str(size).strip()
        if sign is not None:
            if size.startswith(">"):
                return sign * ABOVE_STEP_BP if parse_float(size[1:]) == 25 else None
            value = parse_float(size)
            return None if value is None else int(sign * value)
    match = re.search(r"-(C|H)(\d+)$", str(market.get("ticker", "")))
    if match:  # ticker suffix: C25 = cut 25, C26 = cut >25, H0 = hold
        size = int(match.group(2))
        size = ABOVE_STEP_BP if size == 26 else size
        return -size if match.group(1) == "C" else size
    return None


def parse_kalshi_event(event: dict, markets: list[dict]) -> list[FedMarketInfo]:
    """Outcome markets of one Kalshi FOMC event; markets without a readable rate step are skipped."""
    decided = _parse_time(event.get("strike_date"))
    out = []
    for m in markets:
        if not isinstance(m, dict) or not m.get("ticker"):
            continue
        bp = kalshi_outcome_bp(m)
        session = decided or _parse_time(m.get("close_time"))
        if bp is None or session is None:
            continue
        ticker = str(m["ticker"])
        out.append(FedMarketInfo(KALSHI, ticker, str(event.get("event_ticker") or m.get("event_ticker")), session.date(),
                                 bp, str(m.get("yes_sub_title") or m.get("subtitle") or ticker),
                                 str(m.get("title") or ticker), KALSHI_WEB_URL.format(ticker=ticker), ticker,
                                 _parse_time(m.get("open_time"))))
    return out


def _dollars(block, key: str) -> float | None:
    if not isinstance(block, dict):
        return None
    value = block.get(f"{key}_dollars", block.get(key))
    return parse_float(value)


def kalshi_candle_price(candle: dict) -> float | None:
    """Close of the day; without a trade the bid/ask midpoint, else the previous close."""
    price = _dollars(candle.get("price"), "close")
    if price is not None:
        return price
    bid, ask = _dollars(candle.get("yes_bid"), "close"), _dollars(candle.get("yes_ask"), "close")
    if bid is not None and ask is not None and ask > bid:
        return (bid + ask) / 2
    return _dollars(candle.get("price"), "previous")


def parse_kalshi_candles(payload, now: datetime) -> list[FedPoint]:
    points: dict[date, FedPoint] = {}
    for candle in (payload or {}).get("candlesticks") or []:
        if not isinstance(candle, dict) or candle.get("end_period_ts") is None:
            continue
        price = kalshi_candle_price(candle)
        contracts = parse_float(candle.get("volume_fp", candle.get("volume")))
        observed = datetime.fromtimestamp(int(candle["end_period_ts"]), timezone.utc)
        _keep(points, observed, price, None if contracts is None or price is None else contracts * price, now)
    return sorted(points.values(), key=lambda p: p.day)


# ---- Polymarket -----------------------------------------------------------------------------

def polymarket_outcome_bp(label: str) -> int | None:
    """Rate step of a Polymarket outcome name: "50+ bps decrease" -> -50, "No change" -> 0, "Other" -> None."""
    text = (label or "").strip().lower()
    if text in ("no change", "no changes", "unchanged", "hold"):
        return 0
    match = POLY_OUTCOME.search(text)
    if not match:
        return None
    size = int(match.group(1))
    return -size if match.group(2).lower() in ("decrease", "cut") else size


def _yes_token(market: dict) -> str | None:
    tokens = market.get("clobTokenIds")
    if isinstance(tokens, str):
        try:
            tokens = json.loads(tokens)
        except ValueError:
            return None
    return str(tokens[0]) if isinstance(tokens, list) and tokens else None


def is_fed_decision_event(event: dict) -> bool:
    return isinstance(event, dict) and bool(POLY_TITLE.match(str(event.get("title") or "")))


def snap_session(raw: date, sessions: list[date]) -> date | None:
    """Known meeting closest to ``raw`` within ``SNAP_DAYS``; Polymarket end dates are off by up to a day."""
    near = [s for s in sessions if abs((s - raw).days) <= SNAP_DAYS]
    return min(near, key=lambda s: abs((s - raw).days)) if near else None


def parse_polymarket_event(event: dict, sessions: list[date]) -> list[FedMarketInfo]:
    """Outcome markets of one Polymarket FOMC event; [] if it matches no known meeting date."""
    end = _parse_time(event.get("endDate"))
    if not is_fed_decision_event(event) or end is None:
        return []
    session = snap_session(end.date(), sessions)
    if session is None:
        return []
    out = []
    for m in event.get("markets") or []:
        if not isinstance(m, dict) or m.get("id") is None:
            continue
        label = str(m.get("groupItemTitle") or "")
        bp, token = polymarket_outcome_bp(label), _yes_token(m)
        if bp is None or token is None:
            continue
        out.append(FedMarketInfo(POLYMARKET, str(m["id"]), str(event.get("id")), session, bp, label,
                                 str(m.get("question") or label), POLYMARKET_WEB_URL.format(slug=event.get("slug", "")),
                                 token, _parse_time(m.get("startDate"))))
    return out


def parse_polymarket_history(payload, now: datetime) -> list[FedPoint]:
    points: dict[date, FedPoint] = {}
    for item in (payload or {}).get("history") or []:
        if not isinstance(item, dict) or item.get("t") is None:
            continue
        observed = datetime.fromtimestamp(int(item["t"]), timezone.utc)
        _keep(points, observed, parse_float(item.get("p")), None, now)
    return sorted(points.values(), key=lambda p: p.day)


def start_ts(info: FedMarketInfo, fallback_days: int = 730) -> int:
    """Earliest candle time to ask Kalshi for: the market's opening, else two years before the meeting."""
    opened = info.opened_at or datetime.combine(info.session_date - timedelta(days=fallback_days),
                                                datetime.min.time(), tzinfo=timezone.utc)
    return int((opened - timedelta(days=1)).timestamp())
