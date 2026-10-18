"""Daily price histories of single markets from the public Polymarket and Kalshi APIs, read through a getter.

Answers come from ``get_json(url, params) -> JSON | None`` (None = failed request): inside the raw job "history" the
raw layer's recorder, in the processes the reader of ``raw_responses`` (``rawdata.providers.RawHistory``) - this
module never opens a connection. The volatile window parameters (``VOLATILE``) are no part of the request identity.

* Polymarket: the Gamma market (``/markets/{id}``) names the Yes token; the CLOB
  ``/prices-history`` returns one price per day for it (``interval=max``,
  ``fidelity=1440`` minutes). Daily volume is not published, so the caller keeps
  the market's current 24h volume as liquidity proxy (``volume_usd`` is None). Resolved markets often answer
  ``interval=max`` with an empty history; then the explicit window ``startTs``/``endTs`` is asked once more.
* Kalshi: ``/series/{series}/markets/{ticker}/candlesticks`` with daily periods
  gives close price and traded contracts; contracts x price is the USD turnover
  (same conversion as the live client). Without a trade the bid/ask midpoint,
  else the previous close is used. The series is the ticker's first segment. Markets settled before Kalshi's
  historical cutoff are only served by ``/historical/markets/{ticker}/candlesticks``, asked when the first answer is empty.

Every point is dated by the UTC date of its timestamp, i.e. the state seen at the
start of that day, like a daily snapshot. Points observed after the US-close cutoff of
their day are dropped (point-in-time rule, ``quality.pit``), so no backfilled value
carries information from after the close it is paired with. ``fetch_history`` returns {} on any error.
"""

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from backend.app.clients.base import GetJson, parse_float
from backend.app.quality.pit import is_observable
from backend.app.rawdata.access import MissingRawData

logger = logging.getLogger(__name__)

GAMMA_MARKET_URL = "https://gamma-api.polymarket.com/markets/{id}"
CLOB_HISTORY_URL = "https://clob.polymarket.com/prices-history"
KALSHI_CANDLES_URL = "https://api.elections.kalshi.com/trade-api/v2/series/{series}/markets/{ticker}/candlesticks"
KALSHI_HISTORICAL_CANDLES_URL = "https://api.elections.kalshi.com/trade-api/v2/historical/markets/{ticker}/candlesticks"
DAY_MINUTES = 1440
VOLATILE = frozenset({"startTs", "endTs", "start_ts", "end_ts"})


@dataclass(frozen=True)
class DayPoint:
    price: float
    volume_usd: float | None = None  # None: the source publishes no daily volume


def _day(ts) -> date | None:
    """UTC day of an observation, or None if it lies after that day's US-close cutoff."""
    observed = datetime.fromtimestamp(int(ts), timezone.utc)
    return observed.date() if is_observable(observed, observed.date()) else None


def parse_polymarket_history(payload) -> dict[date, DayPoint]:
    out: dict[date, DayPoint] = {}
    items = [i for i in (payload or {}).get("history") or [] if isinstance(i, dict) and i.get("t") is not None]
    for item in sorted(items, key=lambda i: int(i["t"])):
        price, day = parse_float(item.get("p")), _day(item["t"])
        if price is not None and day is not None:
            out[day] = DayPoint(price)  # several points per day: the last one before the cutoff wins
    return out


def yes_token(market) -> str | None:
    tokens = market.get("clobTokenIds") if isinstance(market, dict) else None
    if isinstance(tokens, str):
        try:
            tokens = json.loads(tokens)
        except ValueError:
            return None
    return str(tokens[0]) if isinstance(tokens, list) and tokens else None


def fetch_polymarket(external_id: str, start: date, get: GetJson) -> dict[date, DayPoint]:
    token = yes_token(get(GAMMA_MARKET_URL.format(id=external_id), {}))
    if token is None:
        return {}
    history = parse_polymarket_history(get(CLOB_HISTORY_URL, {"market": token, "interval": "max", "fidelity": DAY_MINUTES}))
    if not history:  # resolved markets: explicit window instead of "max"
        window = {"startTs": _ts(start), "endTs": int(datetime.now(timezone.utc).timestamp())}
        history = parse_polymarket_history(get(CLOB_HISTORY_URL, {"market": token, "fidelity": DAY_MINUTES, **window}))
    return {d: p for d, p in history.items() if d >= start}


def _ts(day: date) -> int:
    return int(datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc).timestamp())


def _close(block) -> float | None:
    return parse_float(block.get("close_dollars")) if isinstance(block, dict) else None


def candle_price(candle: dict) -> float | None:
    price = _close(candle.get("price"))
    if price is not None:
        return price
    bid, ask = _close(candle.get("yes_bid")), _close(candle.get("yes_ask"))
    if bid is not None and ask is not None:
        return (bid + ask) / 2
    block = candle.get("price")
    return parse_float(block.get("previous_dollars")) if isinstance(block, dict) else None


def parse_kalshi_candles(payload) -> dict[date, DayPoint]:
    out: dict[date, DayPoint] = {}
    for candle in (payload or {}).get("candlesticks") or []:
        if not isinstance(candle, dict) or candle.get("end_period_ts") is None:
            continue
        price = candle_price(candle)
        if price is None:
            continue
        day = _day(candle["end_period_ts"])
        if day is None:
            continue
        contracts = parse_float(candle.get("volume_fp", candle.get("volume"))) or 0.0
        out[day] = DayPoint(price, contracts * price)
    return out


def fetch_kalshi(ticker: str, start: date, get: GetJson) -> dict[date, DayPoint]:
    params = {"start_ts": _ts(start - timedelta(days=1)), "end_ts": int(datetime.now(timezone.utc).timestamp()), "period_interval": DAY_MINUTES}
    candles = parse_kalshi_candles(get(KALSHI_CANDLES_URL.format(series=ticker.split("-")[0], ticker=ticker), params))
    if not candles:  # failed, or settled before the historical cutoff: archived endpoint
        logger.debug("kalshi candles of %s empty or failed; trying the historical endpoint", ticker)
        candles = parse_kalshi_candles(get(KALSHI_HISTORICAL_CANDLES_URL.format(ticker=ticker), params))
    return {d: p for d, p in candles.items() if d >= start}


FETCHERS = {"polymarket": fetch_polymarket, "kalshi": fetch_kalshi}


def _get(url: str, params: dict):
    """Default getter: there is no live access here; histories come from the raw data layer."""
    raise MissingRawData(f"Kein Rohdaten-Zugang für {url}: Preis-Historien kommen aus der Rohdatenschicht (rawdata.providers.RawHistory)")


def fetch_history(source: str, external_id: str, start: date, get_json: GetJson | None = None) -> dict[date, DayPoint]:
    """Daily points of one market from ``start`` on; {} for unknown sources or on any error (``MissingRawData`` passes)."""
    fetcher = FETCHERS.get(source)
    if fetcher is None:
        return {}
    try:
        return fetcher(external_id, start, get_json or _get)
    except (ValueError, TypeError, AttributeError) as exc:
        logger.warning("history of %s/%s failed: %s", source, external_id, exc)
        return {}
