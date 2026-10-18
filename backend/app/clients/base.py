"""Shared schema and request plumbing for market data clients (no HTTP of their own: answers come from a getter)."""

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

from backend.app.models import MIN_LIQUIDITY_USD, utcnow

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 10.0

GetJson = Callable[[str, dict], object]


@dataclass(frozen=True)
class NormalizedMarket:
    """One market in the common schema, identical across all sources.

    Fields mirror the ``markets`` table (see backend.app.models.Market).
    ``tags`` carries the source's categories/labels (several per market);
    ``category`` stays as the first tag for backwards compatibility.

    Order-book fields (``best_bid``/``best_ask`` as 0-1 prices) and
    ``volume_24h`` (USD) feed the Economic Uncertainty Index; ``active``
    is the source's own status flag (None when the source reports none).
    ``description`` is the source's resolution text / rules, searchable in
    the Index Manufacture catalog. ``liquidity`` is the 24h turnover in USD
    (identical to ``volume_24h``; comparable across both sources), so floor,
    filters, ranking and weights all rest on it; ``liquidity_basis`` is then
    ``volume_24h`` (see ``backend.app.field_docs``). ``depth`` keeps the
    source's standing-money figure (Polymarket order-book depth, Kalshi
    reported liquidity or open interest) for the uncertainty liquidity score.
    ``event_key``/``event_title`` name the source event the market belongs
    to (several outcome markets of one election share it) and
    ``outcome_label`` the market's outcome within it; None when the source
    reports no event (see ``backend.app.events``). They are internal: the
    index endpoints strip them (``EVENT_FIELDS``) to keep their fields stable.
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
    description: str | None = None
    liquidity_basis: str | None = None
    depth: float | None = None
    event_key: str | None = None
    event_title: str | None = None
    outcome_label: str | None = None


EVENT_FIELDS = ("event_key", "event_title", "outcome_label")


LIQUIDITY_BASIS_VOLUME_24H = "volume_24h"


def basis_of(volume_24h: float | None) -> str | None:
    """Basis tag of the liquidity figure: 24h turnover when known, else None."""
    return LIQUIDITY_BASIS_VOLUME_24H if volume_24h is not None else None


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


def clean_text(value, limit: int = 2000) -> str | None:
    """Trimmed free text capped at ``limit`` characters; None when missing or empty."""
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()[:limit]


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
    """Pages through a source's answers and never lets one bad response crash the caller.

    Answers come from ``get_json(url, params) -> JSON | None``: inside a raw job the raw layer's recorder
    (API -> staging -> ``raw_responses``), in the processes the reader of ``raw_responses``
    (``backend.app.rawdata.providers.RawMarkets``); the client itself never opens a connection. An injected
    HTTP-like ``session`` (tests, tools) is wrapped by ``rawdata.access.session_getter``.

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
        session=None,
        timeout: float = DEFAULT_TIMEOUT,
        liquidity_floor: float | None = MIN_LIQUIDITY_USD,
        request_delay: float = 0.0,
        get_json: GetJson | None = None,
    ):
        if get_json is None and session is not None:
            from backend.app.rawdata.access import session_getter

            get_json = session_getter(session, timeout, self.source)
        self._get = get_json
        self.liquidity_floor = liquidity_floor
        self.request_delay = request_delay

    def _get_json(self, url: str, params: dict | None = None) -> dict | list | None:
        """Parsed answer of one request, None when it failed (``MissingRawData`` when nothing is stored at all)."""
        if self._get is None:
            raise RuntimeError(f"{self.source}: no data access - markets come from the raw data layer (rawdata.providers)")
        if self.request_delay > 0:
            time.sleep(self.request_delay)
        return self._get(url, params or {})

    def fetch_markets(self) -> list[NormalizedMarket]:
        """Normalized markets at or above the liquidity floor; empty if the API is unavailable."""
        markets: list[NormalizedMarket] = []
        floor = self.liquidity_floor or 0.0
        for item in self._fetch_raw():
            market = self._safe_normalize(item)
            if market is not None and (market.liquidity or 0.0) >= floor:
                markets.append(market)
        logger.info("%s: fetched %d markets (floor %.0f USD 24h volume)", self.source, len(markets), floor)
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
