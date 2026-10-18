"""Turn stored raw answers of the market listings (``raw_responses``, datasets ``directory_*``) into directory entries.

Pure parsing, no network: the answers are fetched by the raw data layer (``backend.app.rawdata.streams``).
Polymarket answers are event lists (or ``{"events": [...]}`` from the keyset listing), Kalshi answers are
``{"events": [...]}`` with nested markets or ``{"markets": [...]}``. A malformed market yields None (counted by the caller);
an answer of unexpected shape raises ``BrokenAnswer``. Every entry carries its market group (Polymarket event, Kalshi
event ticker): the hundreds of brackets of one question (e.g. S&P 500 ranges) form one group market.
"""

import json
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime

from backend.app.clients.base import clean_tags, parse_datetime, parse_float
from backend.app.directory.models import ACTIVE, EXPIRED, RESOLVED
from backend.app.quality.pit import as_utc

KALSHI_RESOLVED = {"settled", "finalized", "determined"}


class BrokenAnswer(ValueError):
    """A raw answer that is no JSON or has no market list."""


@dataclass(frozen=True)
class Entry:
    source: str
    external_id: str
    title: str
    tags: list[str]
    listed_at: datetime | None
    end_date: datetime | None
    status: str
    group_key: str | None = None    # Polymarket event id / Kalshi event ticker
    group_title: str | None = None  # event title (unknown for Kalshi /markets answers)


def _dt(value) -> datetime | None:
    """ISO date/time as UTC (date-only and zone-less values are UTC)."""
    return as_utc(parse_datetime(value))


def _status(resolved: bool, closed: bool, end: datetime | None, now: datetime) -> str:
    if resolved:
        return RESOLVED
    return EXPIRED if closed or (end is not None and end <= now) else ACTIVE


def polymarket_entries(event, now: datetime) -> Iterator[Entry | None]:
    """Markets of one Gamma event; None for a malformed market or event."""
    if not isinstance(event, dict):
        yield None
        return
    tags = clean_tags(event.get("tags"))
    group = str(event["id"]) if event.get("id") else None
    group_title = str(event["title"])[:512] if event.get("title") else None
    for m in event.get("markets") or []:
        if not isinstance(m, dict) or not m.get("id") or not m.get("question"):
            yield None
            continue
        end = _dt(m.get("endDate")) or _dt(event.get("endDate"))
        prices = m.get("outcomePrices")
        try:
            prices = json.loads(prices) if isinstance(prices, str) else prices
        except ValueError:
            prices = None
        decided = isinstance(prices, list) and any(parse_float(p) == 1.0 for p in prices)
        closed = bool(m.get("closed"))
        resolved = closed and (m.get("umaResolutionStatus") == "resolved" or decided)
        yield Entry("polymarket", str(m["id"]), str(m["question"])[:512], tags,
                    _dt(m.get("startDate")) or _dt(m.get("createdAt")) or _dt(event.get("startDate")),
                    end, _status(resolved, closed or m.get("active") is False, end, now), group, group_title)


def kalshi_entry(m, tags: list[str], now: datetime, fallback_title=None, event=None) -> Entry | None:
    if not isinstance(m, dict) or not m.get("ticker") or not (m.get("title") or fallback_title):
        return None
    status = str(m.get("status") or "").lower()
    end = _dt(m.get("close_time"))
    resolved = status in KALSHI_RESOLVED or str(m.get("result") or "").lower() in ("yes", "no")
    event = event or {}
    group = event.get("event_ticker") or m.get("event_ticker")
    return Entry("kalshi", str(m["ticker"]), str(m.get("title") or fallback_title)[:512], tags,
                 _dt(m.get("open_time")), end, _status(resolved, status == "closed", end, now),
                 str(group) if group else None, str(event["title"])[:512] if event.get("title") else None)


def kalshi_event_entries(event, now: datetime) -> Iterator[Entry | None]:
    if not isinstance(event, dict):
        yield None
        return
    tags = clean_tags([event.get("category")])
    for m in event.get("markets") or []:
        yield kalshi_entry(m, tags, now, event.get("title"), event)


def answer_entries(source: str, dataset: str, content: str, now: datetime) -> Iterator[Entry | None]:
    """All entries of one stored answer; raises ``BrokenAnswer`` before yielding when the answer is unusable."""
    try:
        data = json.loads(content)
    except ValueError as exc:
        raise BrokenAnswer(f"no JSON: {exc}") from exc
    markets = source == "kalshi" and dataset != "directory_events"
    items = data.get("markets" if markets else "events") if isinstance(data, dict) else data
    if not isinstance(items, list):
        raise BrokenAnswer(f"no market list ({type(data).__name__})")
    return _entries(source, markets, items, now)


def _entries(source: str, markets: bool, items: list, now: datetime) -> Iterator[Entry | None]:
    for item in items:
        if source == "polymarket":
            yield from polymarket_entries(item, now)
        elif markets:
            yield kalshi_entry(item, [], now)
        else:
            yield from kalshi_event_entries(item, now)
