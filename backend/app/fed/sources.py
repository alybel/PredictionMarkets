"""HTTP access to the Fed decision markets of Kalshi and Polymarket.

``HttpFedSource`` lists every FOMC outcome market of both sources (open and settled) and fetches its daily
history. The JSON getter is injectable, so tests feed fixture payloads through the real code without network.
Kalshi moves settled markets to its historical API after a cutoff; both endpoints are tried.
"""

import logging
import time
from collections.abc import Callable
from datetime import datetime

import requests

from backend.app.clients.base import parse_float
from backend.app.fed.parsers import (FedMarketInfo, FedPoint, KALSHI, POLYMARKET, is_fed_decision_event,
                                     parse_kalshi_candles, parse_kalshi_event, parse_polymarket_event,
                                     parse_polymarket_history, start_ts)

logger = logging.getLogger(__name__)

KALSHI_API = "https://api.elections.kalshi.com/trade-api/v2"
KALSHI_SERIES = "KXFEDDECISION"
GAMMA_EVENTS_URL = "https://gamma-api.polymarket.com/events"
CLOB_HISTORY_URL = "https://clob.polymarket.com/prices-history"
POLY_TAG = "fed-rates"
PAGE = 100
MAX_PAGES = 20
DAY_MINUTES = 1440
TIMEOUT_S = 20

JsonGetter = Callable[[str, dict | None], object]


RETRIES = 4
BACKOFF_S = 2.0


def _request(url: str, params: dict | None, sleep: Callable[[float], None]) -> requests.Response:
    """GET; on HTTP 429 (rate limit) waits ``Retry-After`` or an increasing backoff and retries."""
    for attempt in range(RETRIES + 1):
        response = requests.get(url, params=params, timeout=TIMEOUT_S)
        if response.status_code != 429 or attempt == RETRIES:
            break
        wait = parse_float(response.headers.get("Retry-After")) or BACKOFF_S * 2 ** attempt
        logger.info("rate limited by %s, retry %d in %.1f s", url.split("?")[0], attempt + 1, wait)
        sleep(wait)
    response.raise_for_status()
    return response


def http_get(url: str, params: dict | None = None, sleep: Callable[[float], None] = time.sleep):
    """GET JSON (with rate-limit retry)."""
    return _request(url, params, sleep).json()


def http_get_raw(url: str, params: dict | None = None, sleep: Callable[[float], None] = time.sleep) -> bytes:
    """GET the unchanged response body (with rate-limit retry); used by the staging fetch."""
    return _request(url, params, sleep).content


class HttpFedSource:
    def __init__(self, get: JsonGetter = http_get):
        self.get = get

    # ---- listing ----
    def kalshi_markets(self) -> list[FedMarketInfo]:
        events, cursor = [], None
        for _ in range(MAX_PAGES):
            params = {"series_ticker": KALSHI_SERIES, "limit": 200, **({"cursor": cursor} if cursor else {})}
            data = self.get(f"{KALSHI_API}/events", params) or {}
            events.extend(data.get("events") or [])
            cursor = data.get("cursor")
            if not cursor:
                break
        out = []
        for event in events:
            ticker = event.get("event_ticker")
            try:
                markets = (self.get(f"{KALSHI_API}/markets", {"event_ticker": ticker, "limit": 50}) or {}).get("markets") or []
                if not markets:  # settled before the historical cutoff
                    markets = (self.get(f"{KALSHI_API}/historical/markets", {"event_ticker": ticker, "limit": 50}) or {}).get("markets") or []
                out.extend(parse_kalshi_event(event, markets))
            except (requests.RequestException, ValueError, TypeError, AttributeError) as exc:
                logger.warning("fed listing of kalshi event %s failed: %s", ticker, exc)
        logger.info("fed listing kalshi: %d events, %d outcome markets", len(events), len(out))
        return out

    def polymarket_markets(self, sessions: list) -> list[FedMarketInfo]:
        events = []
        for page in range(MAX_PAGES):
            batch = self.get(GAMMA_EVENTS_URL, {"tag_slug": POLY_TAG, "limit": PAGE, "offset": page * PAGE, "order": "id"}) or []
            events.extend(e for e in batch if is_fed_decision_event(e))
            if len(batch) < PAGE:
                break
        out, unmatched = [], []
        for event in events:
            parsed = parse_polymarket_event(event, sessions)
            out.extend(parsed)
            if not parsed:
                unmatched.append(event.get("slug"))
        if unmatched:
            logger.warning("fed listing polymarket: %d events without a known meeting date skipped: %s", len(unmatched), unmatched)
        logger.info("fed listing polymarket: %d events, %d outcome markets", len(events), len(out))
        return out

    def list_markets(self) -> list[FedMarketInfo]:
        """Kalshi first: its meeting dates anchor the Polymarket events. A failing source is logged, not raised."""
        kalshi: list[FedMarketInfo] = []
        try:
            kalshi = self.kalshi_markets()
        except (requests.RequestException, ValueError, TypeError, AttributeError) as exc:
            logger.error("fed listing kalshi failed: %s", exc)
        try:
            poly = self.polymarket_markets(sorted({m.session_date for m in kalshi}))
        except (requests.RequestException, ValueError, TypeError, AttributeError) as exc:
            logger.error("fed listing polymarket failed: %s", exc)
            poly = []
        return kalshi + poly

    # ---- history ----
    def history(self, info: FedMarketInfo, now: datetime) -> list[FedPoint]:
        """Daily points of one outcome market; raises on a broken answer (the loader skips the market)."""
        if info.source == KALSHI:
            params = {"start_ts": start_ts(info), "end_ts": int(now.timestamp()), "period_interval": DAY_MINUTES}
            points: list[FedPoint] = []
            try:
                points = parse_kalshi_candles(self.get(f"{KALSHI_API}/series/{KALSHI_SERIES}/markets/{info.history_key}/candlesticks", params), now)
            except requests.HTTPError:
                points = []
            return points or parse_kalshi_candles(self.get(f"{KALSHI_API}/historical/markets/{info.history_key}/candlesticks", params), now)
        if info.source == POLYMARKET:
            return parse_polymarket_history(self.get(CLOB_HISTORY_URL, {"market": info.history_key, "interval": "max", "fidelity": DAY_MINUTES}), now)
        raise ValueError(f"unknown source {info.source!r}")
