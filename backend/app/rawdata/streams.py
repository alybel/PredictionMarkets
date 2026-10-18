"""Market listing streams of both sources (open, expired, resolved), fetched page by page into the staging folder.

Streams (checked live 2026-10-17):

* ``polymarket_new``: Gamma ``/events?closed=false`` newest listing first (new listings). Head stream.
* ``polymarket_closed``: Gamma ``/events?closed=true`` latest closing first (status changes). Head stream.
* ``polymarket_all``: Gamma ``/events/keyset`` (open and closed events, oldest id first, ``after_cursor``). Resumable:
  each run continues at the saved cursor, so the full list builds up over runs and new events follow at the end.
* ``kalshi_new`` / ``kalshi_settled``: ``/markets`` created / settled since the last run (``min_created_ts`` /
  ``min_settled_ts``, without combo markets).
* ``kalshi_all``: ``/events`` with nested markets, no status filter (open, closed, settled). Resumable; restarts when done.
* ``kalshi_historical``: ``/historical/markets`` without combo markets (settled before Kalshi's archive cutoff). Resumable.

A head stream stops at the first page whose answer equals the stored one (nothing new); a "since" stream ends with its
listing. Pages per stream and run are capped. Every successful answer is written unchanged to
``data/staging/<source>/directory_*`` (``Stager``: never overwritten, an equal answer is not written again). Requests
go through an injectable ``get_raw(url, params) -> bytes | None`` (None = failed request), so tests never reach the
network. Failed, empty or malformed answers end the affected stream, are counted and logged, and never stop the others.
"""

import json
import logging
import os
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime

import requests

from backend.app.clients.kalshi import KALSHI_EVENTS_URL
from backend.app.clients.polymarket import GAMMA_EVENTS_URL
from backend.app.staging.files import Stager

logger = logging.getLogger(__name__)

GAMMA_KEYSET_URL = GAMMA_EVENTS_URL + "/keyset"
KALSHI_MARKETS_URL = "https://api.elections.kalshi.com/trade-api/v2/markets"
KALSHI_HISTORICAL_URL = "https://api.elections.kalshi.com/trade-api/v2/historical/markets"
MAX_PAGES = int(os.environ.get("DIRECTORY_MAX_PAGES", "100"))  # per resumable stream and run
HEAD_PAGES = 20
TIMEOUT_S = 30
REQUEST_DELAY_S = 0.1
HEADERS = {"User-Agent": "Mozilla/5.0 (PredictionMarketIndex)"}  # Yahoo refuses requests without one

GetRaw = Callable[[str, dict], bytes | None]


@dataclass(frozen=True)
class Stream:
    name: str
    source: str
    dataset: str
    url: str
    params: dict
    key: str | None         # list key in the answer; None: the answer is the list
    cursor_in: str | None   # request parameter of the cursor; None: offset paging
    cursor_out: str | None  # answer field with the next cursor
    head: bool              # stop at the first unchanged page; always starts at the top
    keep_cursor: bool = False  # at the end keep the last cursor (append-only list) instead of restarting
    page_size: int = 100
    since: str | None = None   # request parameter taking the unix time of the last run; always starts at the top

    @property
    def resumable(self) -> bool:
        return not self.head and not self.since


_KALSHI_MARKETS = {"limit": 1000, "mve_filter": "exclude"}
STREAMS = (
    Stream("polymarket_new", "polymarket", "directory_events", GAMMA_EVENTS_URL,
           {"closed": "false", "order": "startDate", "ascending": "false", "limit": 100}, None, None, None, head=True),
    Stream("polymarket_closed", "polymarket", "directory_events", GAMMA_EVENTS_URL,
           {"closed": "true", "order": "closedTime", "ascending": "false", "limit": 100}, None, None, None, head=True),
    Stream("polymarket_all", "polymarket", "directory_events", GAMMA_KEYSET_URL, {"limit": 100}, "events", "after_cursor", "next_cursor",
           head=False, keep_cursor=True),
    Stream("kalshi_all", "kalshi", "directory_events", KALSHI_EVENTS_URL, {"with_nested_markets": "true", "limit": 200}, "events",
           "cursor", "cursor", head=False, page_size=200),
    Stream("kalshi_new", "kalshi", "directory_markets_created", KALSHI_MARKETS_URL, _KALSHI_MARKETS, "markets", "cursor", "cursor",
           head=False, page_size=1000, since="min_created_ts"),
    Stream("kalshi_settled", "kalshi", "directory_markets_settled", KALSHI_MARKETS_URL, _KALSHI_MARKETS, "markets", "cursor", "cursor",
           head=False, page_size=1000, since="min_settled_ts"),
    Stream("kalshi_historical", "kalshi", "directory_historical_markets", KALSHI_HISTORICAL_URL, _KALSHI_MARKETS, "markets",
           "cursor", "cursor", head=False, page_size=1000),
)
DATASETS = frozenset((s.source, s.dataset) for s in STREAMS)


@dataclass
class SourceStats:
    errors: int = 0
    requests: int = 0
    written: int = 0
    error_details: list[str] = field(default_factory=list)


def http_get_raw(url: str, params: dict) -> bytes | None:
    """The only live GET of the app's business data (directory, markets, domains, histories, stock closes) with a small
    pause (rate limits); None on any error."""
    time.sleep(REQUEST_DELAY_S)
    try:
        response = requests.get(url, params=params, headers=HEADERS, timeout=TIMEOUT_S)
        response.raise_for_status()
        return response.content
    except requests.RequestException as exc:
        logger.warning("raw fetch: request to %s failed: %s", url, exc)
        return None


class Crawler:
    """Pages through one stream: stages every answer, yields ``(resume position, finished)`` per page.

    ``files`` collects the staging file of every answer (also unchanged ones), so the consumers know what this run saw."""

    def __init__(self, stager: Stager, get_raw: GetRaw, now: Callable[[], datetime]):
        self.stager, self.get_raw, self.now = stager, get_raw, now
        self.stats: dict[str, SourceStats] = {"polymarket": SourceStats(), "kalshi": SourceStats()}
        self.files: list[str] = []

    def fail(self, source: str, text: str) -> None:
        stats = self.stats[source]
        stats.errors += 1
        stats.error_details.append(text[:200])
        logger.warning("raw fetch %s: %s", source, text)

    def _get(self, s: Stream, params: dict):
        body = self.get_raw(s.url, params)
        stats = self.stats[s.source]
        stats.requests += 1
        if body is None:
            self.fail(s.source, f"request failed: {s.url} {params}")
            return None
        written = True
        try:
            staged = self.stager.write(s.source, s.dataset, s.url, params, body, self.now())
            written = staged.written
            stats.written += written
            self.files.append(staged.path.relative_to(self.stager.root).as_posix())
        except OSError as exc:  # staging is the raw trail; a full disk must be visible
            self.fail(s.source, f"staging write failed: {exc}")
        try:
            data = json.loads(body)
        except ValueError as exc:
            self.fail(s.source, f"answer is no JSON ({s.url} {params}): {exc}")
            return None
        items = data.get(s.key) if s.key and isinstance(data, dict) else data
        if not isinstance(items, list):
            self.fail(s.source, f"unexpected answer ({s.url} {params}): {type(data).__name__}")
            return None
        return data, items, written

    def pages(self, s: Stream, start, max_pages: int, since: int | None = None) -> Iterator[tuple[object, bool]]:
        """``start``: saved cursor or offset (None = top); ``since``: unix time for a "since" stream.
        The resume position is where the next run continues."""
        position = start
        for page in range(max_pages):
            params = {**s.params, **({s.since: since} if s.since else {})}
            if s.cursor_in:
                params.update({s.cursor_in: position} if position else {})
            else:
                params["offset"] = position or 0
            got = self._get(s, params)
            if got is None:
                return
            data, items, written = got
            if page == 0 and not items and not position and not s.since:  # nothing created since the last run is normal
                self.fail(s.source, f"empty answer: {s.url} {params}")
            if s.cursor_out:
                nxt = data.get(s.cursor_out) if isinstance(data, dict) else None
            else:
                nxt = (position or 0) + len(items) if len(items) >= s.page_size else None
            done = not nxt or not items or (s.head and not written)
            yield ((position if s.keep_cursor else None) if done else nxt), done
            if done:
                return
            position = nxt
