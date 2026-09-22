"""Shared schema and HTTP plumbing for market data clients."""

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime

import requests

from backend.app.models import MIN_LIQUIDITY_USD, utcnow

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 10.0


@dataclass(frozen=True)
class NormalizedMarket:
    """One market in the common schema, identical across all sources.

    Fields mirror the ``markets`` table (see backend.app.models.Market).
    ``tags`` carries the source's categories/labels (several per market);
    ``category`` stays as the first tag for backwards compatibility.

    Order-book fields (``best_bid``/``best_ask`` as 0-1 prices) and
    ``volume_24h`` (USD) feed the Economic Uncertainty Index; ``active``
    is the source's own status flag (None when the source reports none).
    """

    source: str
    external_id: str
    title: str
    category: str | None
    price: float | None
    volume: float | None
    liquidity: float | None
    end_date: datetime | None = None
    url: str | None = None
    tags: list[str] = field(default_factory=list)
    fetched_at: datetime = field(default_factory=utcnow)
    volume_24h: float | None = None
    best_bid: float | None = None
    best_ask: float | None = None
    active: bool | None = None


def parse_datetime(value) -> datetime | None:
    """Best-effort ISO-8601 parsing; None for missing or malformed values."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def parse_float(value) -> float | None:
    """Best-effort float conversion; None for missing or malformed values."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def clean_tags(values) -> list[str]:
    """Distinct non-empty string tags in original order."""
    if not isinstance(values, list):
        return []
    seen: list[str] = []
    for value in values:
        label = value.get("label") if isinstance(value, dict) else value
        if isinstance(label, str) and label.strip() and label not in seen:
            seen.append(label.strip())
    return seen


class BaseClient:
    """Fetches JSON from an API and never lets one bad response crash the caller.

    Subclasses page through their source in ``_fetch_raw`` and normalize
    single entries in ``_normalize``. ``fetch_markets`` applies the
    ``liquidity_floor`` (the global floor by default), so illiquid markets
    never enter the population; ``liquidity_floor=None`` loads every market
    (used by the daily uncertainty snapshot). ``request_delay`` pauses
    between page requests to respect the sources' rate limits.
    """

    source: str = "base"
    max_pages: int = 20

    def __init__(
        self,
        session: requests.Session | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        liquidity_floor: float | None = MIN_LIQUIDITY_USD,
        request_delay: float = 0.0,
    ):
        self._session = session or requests.Session()
        self._timeout = timeout
        self.liquidity_floor = liquidity_floor
        self.request_delay = request_delay

    def _get_json(self, url: str, params: dict | None = None) -> dict | list | None:
        """GET a URL and return parsed JSON, or None on any error."""
        if self.request_delay > 0:
            time.sleep(self.request_delay)
        try:
            response = self._session.get(url, params=params, timeout=self._timeout)
            response.raise_for_status()
            return response.json()
        except (requests.RequestException, ValueError) as exc:
            logger.warning("%s: request to %s failed: %s", self.source, url, exc)
            return None

    def fetch_markets(self) -> list[NormalizedMarket]:
        """Normalized markets at or above the liquidity floor; empty if the API is unavailable."""
        markets: list[NormalizedMarket] = []
        floor = self.liquidity_floor or 0.0
        for item in self._fetch_raw():
            market = self._safe_normalize(item)
            if market is not None and (market.liquidity or 0.0) >= floor:
                markets.append(market)
        logger.info("%s: fetched %d markets (floor %.0f USD)", self.source, len(markets), floor)
        return markets

    def normalize(self, item) -> NormalizedMarket | None:
        """Normalize one raw API entry; None if it is malformed (used by the probe collector)."""
        return self._safe_normalize(item)

    def _safe_normalize(self, item) -> NormalizedMarket | None:
        try:
            return self._normalize(item)
        except (KeyError, TypeError, ValueError) as exc:
            logger.warning("%s: skipping malformed market entry: %s", self.source, exc)
            return None

    def _fetch_raw(self) -> list:
        raise NotImplementedError

    def _normalize(self, item: dict) -> NormalizedMarket | None:
        raise NotImplementedError
