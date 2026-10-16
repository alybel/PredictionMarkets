"""Fetch the Fed decision markets into the staging folder (no database).

Runs the same requests as ``fed.loader`` (listing of both sources, daily history per outcome market), but
every successful answer is written unchanged to ``data/staging/<source>/<dataset>/<date>/``. An answer equal
to the latest file of the same request is not written again. ``end_ts`` (always "now") is not part of the
request identity. Load the files afterwards with ``python -m backend.app.staging.raw_loader``.

CLI: ``python -m backend.app.fed.fetch``
"""

import json
import logging
import time as clock
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from backend.app.fed.parsers import KALSHI, POLYMARKET
from backend.app.fed.sources import CLOB_HISTORY_URL, GAMMA_EVENTS_URL, KALSHI_API, HttpFedSource, http_get_raw
from backend.app.logging_setup import configure_logging
from backend.app.models import utcnow
from backend.app.staging.files import Stager

logger = logging.getLogger("backend.app.fed.fetch")  # stable name also under `python -m`

VOLATILE_PARAMS = frozenset({"end_ts"})
RawGetter = Callable[[str, dict | None], bytes]


def dataset_of(url: str) -> tuple[str, str]:
    """(source, dataset) of a request URL."""
    if url.startswith(KALSHI_API):
        path = url[len(KALSHI_API):]
        historical = path.startswith("/historical/")
        if path.endswith("/candlesticks"):
            return KALSHI, "historical_candlesticks" if historical else "candlesticks"
        if path.endswith("/markets"):
            return KALSHI, "historical_markets" if historical else "markets"
        if path.endswith("/events"):
            return KALSHI, "events"
    if url.startswith(GAMMA_EVENTS_URL):
        return POLYMARKET, "events"
    if url.startswith(CLOB_HISTORY_URL):
        return POLYMARKET, "prices_history"
    raise ValueError(f"no staging dataset for {url}")


class StagingGetter:
    """JSON getter for ``HttpFedSource`` that stages every raw answer before parsing it."""

    def __init__(self, stager: Stager, fetch_raw: RawGetter = http_get_raw, now: Callable[[], datetime] = utcnow):
        self.stager, self.fetch_raw, self.now = stager, fetch_raw, now
        self.stats = {"requests": 0, "written": 0, "unchanged": 0}

    def __call__(self, url: str, params: dict | None = None):
        body = self.fetch_raw(url, params)
        source, dataset = dataset_of(url)
        result = self.stager.write(source, dataset, url, params, body, self.now())
        self.stats["requests"] += 1
        self.stats["written" if result.written else "unchanged"] += 1
        return json.loads(body)


def fetch_fed(root: Path | None = None, fetch_raw: RawGetter = http_get_raw, now: Callable[[], datetime] = utcnow) -> dict:
    started = clock.monotonic()
    getter = StagingGetter(Stager(root, VOLATILE_PARAMS), fetch_raw, now)
    logger.info("fed fetch to staging started: %s", getter.stager.root)
    source = HttpFedSource(getter)
    infos = source.list_markets()
    failures = []
    for info in infos:
        try:
            source.history(info, now())
        except Exception as exc:  # one broken market must never stop the run
            failures.append({"source": info.source, "external_id": info.external_id, "error": str(exc)[:200]})
            logger.warning("fed fetch of %s/%s failed: %s", info.source, info.external_id, exc)
    summary = {"markets": len(infos), **getter.stats, "failed": len(failures), "failures": failures,
               "duration_s": round(clock.monotonic() - started, 1)}
    logger.info("fed fetch finished: %d markets, %d requests, %d files written, %d unchanged, %d failed in %.1f s",
                summary["markets"], summary["requests"], summary["written"], summary["unchanged"], summary["failed"], summary["duration_s"])
    return summary


def main() -> None:
    configure_logging()
    summary = fetch_fed()
    print({k: v for k, v in summary.items() if k != "failures"})


if __name__ == "__main__":
    main()
