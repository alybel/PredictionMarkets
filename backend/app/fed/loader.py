"""Load the raw layer of the Fed decision markets (append-only, idempotent).

Every outcome market of every FOMC meeting of both sources gets one ``fed_markets`` row and one
``fed_prices`` row per day its source has a price for. Existing rows are never changed; a repeated run only
adds missing markets and days. A market whose history fails is logged and skipped, the run goes on.

CLI: ``python -m backend.app.fed.loader``
"""

import logging
import time as clock
from datetime import datetime
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.fed.models import HISTORICAL, LIVE, FedMarket, FedPrice
from backend.app.fed.parsers import FedMarketInfo, FedPoint
from backend.app.logging_setup import configure_logging
from backend.app.models import utcnow
from backend.app.quality.pit import as_utc, is_observable

logger = logging.getLogger(__name__)


class FedSource(Protocol):
    def list_markets(self) -> list[FedMarketInfo]: ...

    def history(self, info: FedMarketInfo, now: datetime) -> list[FedPoint]: ...


def _market(session: Session, info: FedMarketInfo) -> tuple[FedMarket, bool]:
    row = session.scalar(select(FedMarket).where(FedMarket.source == info.source, FedMarket.external_id == info.external_id))
    if row is not None:
        return row, False
    row = FedMarket(source=info.source, external_id=info.external_id, event_id=info.event_id, session_date=info.session_date,
                    outcome_bp=info.outcome_bp, outcome_label=info.outcome_label[:128], title=info.title[:512], url=info.url)
    session.add(row)
    session.flush()
    return row, True


def store_points(session: Session, market: FedMarket, points: list[FedPoint], now: datetime) -> int:
    """Insert the days not stored yet; points breaking the point-in-time rule are dropped. Returns rows added."""
    stored = set(session.scalars(select(FedPrice.price_date).where(FedPrice.market_id == market.id)))
    rows = [FedPrice(market_id=market.id, price_date=p.day, price=p.price, volume_usd=p.volume_usd, observed_at=p.observed_at,
                     fetched_at=now, origin=LIVE if p.day == now.date() else HISTORICAL)
            for p in points if p.day not in stored and is_observable(p.observed_at, p.day) and as_utc(p.observed_at) <= now]
    session.add_all(rows)
    return len(rows)


def load_fed(session: Session, source: FedSource, now: datetime | None = None) -> dict:
    now = as_utc(now) if now else utcnow()
    started = clock.monotonic()
    logger.info("fed load started at %s", now.isoformat())
    infos = source.list_markets()
    summary = {"markets_listed": len(infos), "markets_new": 0, "markets_loaded": 0, "markets_failed": 0,
               "rows_added": 0, "failures": [], "by_source": {}}
    for info in infos:
        per_source = summary["by_source"].setdefault(info.source, {"markets": 0, "rows_added": 0, "failed": 0})
        per_source["markets"] += 1
        try:
            points = source.history(info, now)
            market, new = _market(session, info)
            added = store_points(session, market, points, now)
            session.commit()
        except Exception as exc:  # one broken market must never stop the run
            session.rollback()
            summary["markets_failed"] += 1
            per_source["failed"] += 1
            summary["failures"].append({"source": info.source, "external_id": info.external_id, "error": str(exc)[:200]})
            logger.warning("fed load of %s/%s failed: %s", info.source, info.external_id, exc)
            continue
        summary["markets_loaded"] += 1
        summary["markets_new"] += int(new)
        summary["rows_added"] += added
        per_source["rows_added"] += added
        logger.debug("fed load %s/%s (%s, %+d bp): %d points, %d new", info.source, info.external_id, info.session_date,
                     info.outcome_bp, len(points), added)
    summary["duration_s"] = round(clock.monotonic() - started, 1)
    logger.info("fed load finished: %d markets listed, %d new, %d loaded, %d failed, %d rows added in %.1f s (%s)",
                summary["markets_listed"], summary["markets_new"], summary["markets_loaded"], summary["markets_failed"],
                summary["rows_added"], summary["duration_s"], summary["by_source"])
    return summary


def main() -> None:
    from backend.app.db import make_engine, make_session_factory
    from backend.app.fed.sources import HttpFedSource

    configure_logging()
    with make_session_factory(make_engine())() as session:
        summary = load_fed(session, HttpFedSource())
        from backend.app.metrics.service import refresh_safely
        refresh_safely(session, "fed load (CLI)")
    print({k: v for k, v in summary.items() if k != "failures"}, f"failures={len(summary['failures'])}")


if __name__ == "__main__":
    main()
