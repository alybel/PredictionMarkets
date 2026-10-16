"""Daily job: snapshot -> classify new markets -> compute today's index value.

Run once a day, e.g. via cron: ``python -m backend.app.eui.daily``.
Options: ``--date YYYY-MM-DD`` (recompute a stored day without fetching),
``--no-classify``, ``--limit N`` (cap new model classifications per run),
``--no-stocks`` (skip loading the closes of the top-100 US stocks for the correlation analysis).
"""

import argparse
import logging
from datetime import date

from sqlalchemy.orm import Session

from backend.app.db import make_engine, make_session_factory
from backend.app.eui.classify import classifier_from_env, classify_missing
from backend.app.eui.compute import compute_index
from backend.app.eui.plausibility import seed_events
from backend.app.eui.reference import seed_reference_indicators
from backend.app.eui.snapshot import snapshot_rows, take_snapshot
from backend.app.logging_setup import configure_logging
from backend.app.stocks.prices import refresh_prices

logger = logging.getLogger(__name__)


def run_daily(session: Session, clients=None, classifier=None, day: date | None = None,
              classify: bool = True, limit: int | None = None, fetch: bool = True) -> dict:
    """Full daily pipeline; returns a summary dict for logs and the API."""
    summary: dict = {}
    if fetch:
        run = take_snapshot(session, clients=clients, snapshot_date=day)
        day = run.snapshot_date
        summary["snapshot"] = {"run_id": run.id, "status": run.status, "source_counts": run.source_counts, "eligible": run.eligible_count}
    if classify:
        seed_reference_indicators(session)
        # Markets without depth or turnover get weight 0 and never become constituents: no need to classify them.
        candidates = [r for r in snapshot_rows(session, day, eligible_only=True) if r.liquidity_score > 0]
        summary["classified"] = classify_missing(session, candidates, classifier or classifier_from_env(), limit=limit)
    seed_events(session)
    value = compute_index(session, day)
    summary["index"] = {"value_date": value.value_date.isoformat(), "value": value.value, "constituents": value.constituent_count}
    logger.info("daily run finished: %s", summary)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Economic Uncertainty Index daily job")
    parser.add_argument("--date", type=date.fromisoformat, help="recompute this stored day (no fetch)")
    parser.add_argument("--no-classify", action="store_true")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--no-stocks", action="store_true")
    args = parser.parse_args()
    configure_logging()
    with make_session_factory(make_engine())() as session:
        summary = run_daily(session, day=args.date, classify=not args.no_classify, limit=args.limit, fetch=args.date is None)
        if not args.no_stocks and args.date is None:
            try:  # stock prices are an add-on: never fail the index run (e.g. migration 0008 not applied yet)
                summary["stocks"] = refresh_prices(session)
            except Exception:
                session.rollback()
                logger.exception("stock price refresh failed")
    print(summary)


if __name__ == "__main__":
    main()
