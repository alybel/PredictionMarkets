"""Find every market of a domain at the sources, open AND expired/resolved ones (chain links need both).

* Polymarket: Gamma ``/events`` filtered by the tag slug of the (sub)topic, once with ``closed=false`` and once
  with ``closed=true``, largest volume first.
* Kalshi: ``/series?category=<topic>`` names the series of the topic (their tags refine it); ``/events`` per series
  without a status filter returns open, closed and settled events with nested markets. Series whose title or
  tags hit a search term come first; the number of series per run is capped.

Requests go through ``get_json(url, params) -> payload | None`` (None = failed request): inside the raw job "domains"
the raw layer's recorder, in the load run the reader of ``raw_responses`` (``rawdata.providers.RawScanner``); this
module never opens a connection. Failed or malformed answers are counted per source and never stop the scan.
"""

import logging
import re
from dataclasses import replace

from backend.app.clients.base import GetJson, clean_tags
from backend.app.clients.kalshi import KALSHI_EVENTS_URL, KalshiClient
from backend.app.clients.polymarket import GAMMA_EVENTS_URL, PolymarketClient

logger = logging.getLogger(__name__)

KALSHI_SERIES_URL = "https://api.elections.kalshi.com/trade-api/v2/series"
POLY_PAGES = 10       # x 100 events per status
KALSHI_SERIES = 60    # series per scan
KALSHI_PAGES = 5      # x 200 events per series


def slug(label: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", label.strip().lower()).strip("-")


class _Counted:
    """get_json wrapper that counts failed (None) or malformed answers."""

    def __init__(self, get_json: GetJson):
        self.get_json, self.errors = get_json, 0

    def __call__(self, url: str, params: dict, expect: type):
        data = self.get_json(url, params)
        if not isinstance(data, expect):
            self.errors += 1
            return None
        return data


def scan_polymarket(topic: str, subtopic: str | None, get_json: GetJson) -> tuple[list, int]:
    client, get = PolymarketClient(liquidity_floor=None), _Counted(get_json)
    items: list = []
    for closed in ("false", "true"):
        for page in range(POLY_PAGES):
            events = get(GAMMA_EVENTS_URL, {"tag_slug": slug(subtopic or topic), "closed": closed, "order": "volume",
                                            "ascending": "false", "limit": 100, "offset": page * 100}, list)
            if not events:
                break
            client._collect(events, items)
            if len(events) < 100:
                break
    return [m for m in map(client.normalize, items) if m is not None], get.errors


def _series(topic: str, terms: list[str], get: _Counted) -> list[dict]:
    data = get(KALSHI_SERIES_URL, {"category": topic}, dict)
    series = [s for s in (data or {}).get("series") or [] if isinstance(s, dict) and s.get("ticker")]
    hit = lambda s: any(t in " ".join([str(s.get("title") or ""), *map(str, s.get("tags") or [])]).lower() for t in terms)
    return sorted(series, key=lambda s: not hit(s))[:KALSHI_SERIES]


def scan_kalshi(topic: str, terms: list[str], get_json: GetJson) -> tuple[list, int]:
    client, get = KalshiClient(liquidity_floor=None), _Counted(get_json)
    markets: list = []
    for series in _series(topic, terms, get):
        tags = clean_tags([topic, *(series.get("tags") or [])])
        cursor = None
        for _ in range(KALSHI_PAGES):
            params = {"series_ticker": series["ticker"], "with_nested_markets": "true", "limit": 200, **({"cursor": cursor} if cursor else {})}
            data = get(KALSHI_EVENTS_URL, params, dict)
            if data is None:
                break
            for event in data.get("events") or []:
                if not isinstance(event, dict):
                    continue
                for raw in event.get("markets") or []:
                    if isinstance(raw, dict):
                        m = client.normalize({**raw, "category": topic, "event_title": event.get("title"),
                                              "event_ticker": raw.get("event_ticker") or event.get("event_ticker")})
                        if m is not None:
                            markets.append(replace(m, tags=tags, category=tags[0] if tags else None))
            cursor = data.get("cursor")
            if not cursor:
                break
    return markets, get.errors


def scan_sources(topic: str, subtopic: str | None, terms: list[str], get_json: GetJson) -> dict[str, dict]:
    """source -> {"markets": [...], "errors": n}; one failing source never stops the other."""
    out: dict[str, dict] = {}
    for source, scan in (("polymarket", lambda: scan_polymarket(topic, subtopic, get_json)),
                         ("kalshi", lambda: scan_kalshi(topic, terms, get_json))):
        try:
            markets, errors = scan()
        except Exception as exc:  # malformed payload deep inside a source answer, or no raw data at all
            logger.exception("domain scan of %s failed: %s", source, exc)
            markets, errors = [], 1
        out[source] = {"markets": markets, "errors": errors}
    return out
