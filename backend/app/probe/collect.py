"""Probe collector: one run captures the configured markets in two batch requests.

Every run writes one JSON file with timestamp, platform, market id, price,
order-book depth and 24h turnover, all in USD (step 1 of the liquidity
harmonization, ``backend.app.eui.harmonize``). Failures are data: HTTP
status (429 included), request errors and missing markets are recorded in
the file instead of aborting the run. The exit code is non-zero whenever
the run was not fully successful, so the Actions log shows it in red.
"""

import argparse
import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import requests

from backend.app.clients.kalshi import KalshiClient
from backend.app.clients.polymarket import PolymarketClient
from backend.app.eui.harmonize import harmonize
from backend.app.logging_setup import configure_logging
from backend.app.models import utcnow
from backend.app.probe import CONFIG_PATH, RUNS_DIR

logger = logging.getLogger(__name__)

KALSHI_MARKETS_URL = "https://api.elections.kalshi.com/trade-api/v2/markets"
GAMMA_MARKETS_URL = "https://gamma-api.polymarket.com/markets"
TIMEOUT = 15.0
SCHEMA_VERSION = 1

# platform -> (client class, batch URL, query builder, id field in the response)
PLATFORMS = {
    "kalshi": (KalshiClient, KALSHI_MARKETS_URL, lambda ids: {"tickers": ",".join(ids)}, "ticker"),
    "polymarket": (PolymarketClient, GAMMA_MARKETS_URL, lambda ids: [("id", i) for i in ids], "id"),
}


@dataclass
class FetchResult:
    http_status: int | None = None
    elapsed_ms: int = 0
    error: str | None = None
    items: list = field(default_factory=list)


def load_config(path: Path = CONFIG_PATH) -> dict[str, list[dict]]:
    """Configured markets per platform; entries without an id are ignored."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return {
        platform: [e for e in (data.get(platform) or []) if isinstance(e, dict) and e.get("id")]
        for platform in PLATFORMS
    }


def fetch_batch(session, url: str, params, timeout: float = TIMEOUT) -> FetchResult:
    """One GET; status code and error text are recorded, never raised."""
    started = time.monotonic()
    result = FetchResult()
    try:
        response = session.get(url, params=params, timeout=timeout)
        result.http_status = response.status_code
        if response.status_code == 429:
            result.error = "rate_limited"
        elif response.status_code >= 400:
            result.error = f"http_{response.status_code}"
        else:
            payload = response.json()
            items = payload.get("markets") if isinstance(payload, dict) else payload
            if isinstance(items, list):
                result.items = items
            else:
                result.error = "unexpected_payload"
    except requests.RequestException as exc:
        result.error = f"request_error: {exc}"[:200]
    except ValueError as exc:
        result.error = f"invalid_json: {exc}"[:200]
    result.elapsed_ms = int((time.monotonic() - started) * 1000)
    return result


def market_row(platform: str, entry: dict, market, now: datetime, error: str | None) -> dict:
    """One market line of the run file; ``market`` is a NormalizedMarket or None."""
    h = harmonize(market, now) if market is not None else None
    return {
        "platform": platform,
        "market_id": str(entry["id"]),
        "topic": entry.get("topic"),
        "title": h.title if h else None,
        "price": h.price if h else None,
        "best_bid": h.best_bid if h else None,
        "best_ask": h.best_ask if h else None,
        "spread": round(h.spread, 6) if h and h.spread is not None else None,
        "depth_usd": h.depth_usd if h else None,
        "volume_24h_usd": h.volume_24h_usd if h else None,
        "active": h.active if h else None,
        "end_date": h.end_date.isoformat() if h and h.end_date else None,
        "error": error,
    }


def collect(session, config: dict[str, list[dict]], now: datetime | None = None, env=None) -> dict:
    """Fetch every configured platform once and build the run record."""
    now = now or utcnow()
    env = os.environ if env is None else env
    record = {
        "schema": SCHEMA_VERSION,
        "timestamp": now.astimezone(timezone.utc).isoformat(timespec="seconds"),
        "run_id": env.get("GITHUB_RUN_ID"),
        "run_attempt": env.get("GITHUB_RUN_ATTEMPT"),
        "status": "ok",
        "sources": {},
        "markets": [],
    }
    failed_sources = missing = 0
    for platform, entries in config.items():
        if not entries:
            continue
        client_cls, url, build_params, id_key = PLATFORMS[platform]
        client = client_cls(session=session, liquidity_floor=None)
        ids = [str(e["id"]) for e in entries]
        result = fetch_batch(session, url, build_params(ids))
        by_id = {str(item.get(id_key)): item for item in result.items if isinstance(item, dict)}
        received = 0
        for entry in entries:
            market = client.normalize(by_id[str(entry["id"])]) if str(entry["id"]) in by_id else None
            error = None if market is not None else (result.error or "not_in_response")
            received += market is not None
            missing += market is None
            record["markets"].append(market_row(platform, entry, market, now, error))
        record["sources"][platform] = {
            "http_status": result.http_status, "elapsed_ms": result.elapsed_ms, "error": result.error,
            "requested": len(ids), "received": received,
        }
        failed_sources += result.error is not None
        logger.info("%s: status=%s received=%d/%d in %d ms%s", platform, result.http_status, received,
                    len(ids), result.elapsed_ms, f" error={result.error}" if result.error else "")
    if failed_sources and failed_sources == len(record["sources"]):
        record["status"] = "failed"
    elif failed_sources or missing:
        record["status"] = "partial"
    return record


def write_record(record: dict, out_dir: Path = RUNS_DIR) -> Path:
    """Write the run record as ``run-<UTC timestamp>-<run id>.json``."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.fromisoformat(record["timestamp"]).strftime("%Y%m%dT%H%M%SZ")
    path = out_dir / f"run-{stamp}-{record.get('run_id') or 'local'}.json"
    path.write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def collect_once(config_path: Path = CONFIG_PATH, out_dir: Path = RUNS_DIR, session=None) -> tuple[dict, Path]:
    record = collect(session or requests.Session(), load_config(config_path))
    path = write_record(record, out_dir)
    logger.info("probe run %s: status=%s file=%s", record.get("run_id") or "local", record["status"], path)
    return record, path


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Probe: fetch the fixed market set once and write a run file")
    parser.add_argument("--config", type=Path, default=CONFIG_PATH)
    parser.add_argument("--out", type=Path, default=RUNS_DIR)
    args = parser.parse_args(argv)
    configure_logging()
    record, path = collect_once(args.config, args.out)
    print(path)
    return 0 if record["status"] == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
