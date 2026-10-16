"""Daily closes of the top-100 US stocks from the public Yahoo Finance chart API, stored incrementally.

A refresh with ``backfill=True`` (button "Kurse laden", CLI ``--backfill``) also loads older closes back to
``HISTORY_DAYS`` for tickers whose stored history starts later (1015-2258); the daily run stays incremental.

Only final closes are stored (``quality.pit``: the day's close is final from 22:00 UTC); a provisional
row (an intraday price stored as close by an older version) is overwritten by the next refresh.

CLI (daily, after the US close): ``python -m backend.app.stocks.prices`` [--tickers AAPL MSFT] [--backfill]
"""

import argparse
import logging
from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone

import requests
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.app.logging_setup import configure_logging
from backend.app.models import utcnow
from backend.app.quality.pit import is_final_close, last_final_day
from backend.app.stocks.models import StockPrice
from backend.app.stocks.universe import TOP_US_STOCKS

logger = logging.getLogger(__name__)

CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
HEADERS = {"User-Agent": "Mozilla/5.0 (PredictionMarketIndex)"}
HISTORY_DAYS = 1860  # a bit more than the longest history window (MAX_DAYS = 5 years)
INITIAL_DAYS = HISTORY_DAYS  # first load
GAP_TOLERANCE_DAYS = 7  # older stored start than this behind the target counts as complete (weekends, holidays)
TIMEOUT_S = 15

Fetcher = Callable[[str, date], dict[date, float]]


def fetch_closes(ticker: str, start: date) -> dict[date, float]:
    """Adjusted daily closes from ``start`` until today; dates in the exchange's time zone."""
    period1 = int(datetime.combine(start, datetime.min.time(), tzinfo=timezone.utc).timestamp())
    params = {"period1": period1, "period2": int(datetime.now(timezone.utc).timestamp()), "interval": "1d"}
    response = requests.get(CHART_URL.format(ticker=ticker), params=params, headers=HEADERS, timeout=TIMEOUT_S)
    response.raise_for_status()
    return parse_chart(response.json())


def parse_chart(payload: dict) -> dict[date, float]:
    result = ((payload.get("chart") or {}).get("result") or [None])[0]
    if not result or not result.get("timestamp"):
        return {}
    offset = timedelta(seconds=(result.get("meta") or {}).get("gmtoffset", 0))
    indicators = result.get("indicators") or {}
    adjusted = (indicators.get("adjclose") or [{}])[0].get("adjclose")
    closes = adjusted or (indicators.get("quote") or [{}])[0].get("close") or []
    out: dict[date, float] = {}
    for ts, close in zip(result["timestamp"], closes):
        if close is not None:
            out[(datetime.fromtimestamp(ts, timezone.utc) + offset).date()] = float(close)
    return out


def _provisional_since(session: Session) -> dict[str, date]:
    """Earliest provisional close per ticker (fetched before the close of its day was final)."""
    out: dict[str, date] = {}
    for ticker, day, fetched in session.execute(select(StockPrice.ticker, StockPrice.price_date, StockPrice.fetched_at)):
        if not is_final_close(day, fetched) and day < out.get(ticker, date.max):
            out[ticker] = day
    return out


def _first_stored(session: Session) -> dict[str, date]:
    return dict(session.execute(select(StockPrice.ticker, func.min(StockPrice.price_date)).group_by(StockPrice.ticker)).all())


def refresh_prices(session: Session, fetcher: Fetcher = fetch_closes, tickers=None, today: date | None = None,
                   now: datetime | None = None, backfill: bool = False) -> dict:
    """Load missing final closes per ticker (from its last stored or first provisional day on) and repair
    provisional rows; one failing ticker never stops the run. ``backfill`` also reaches back to HISTORY_DAYS
    for tickers whose stored history starts later (still one request per ticker)."""
    tickers = list(tickers or TOP_US_STOCKS)
    now = now or utcnow()
    today = today or now.date()
    final_until = last_final_day(now)
    last = dict(session.execute(select(StockPrice.ticker, func.max(StockPrice.price_date)).group_by(StockPrice.ticker)).all())
    provisional = _provisional_since(session)
    target = today - timedelta(days=INITIAL_DAYS)
    first = _first_stored(session) if backfill else {}
    summary = {"tickers": len(tickers), "rows_added": 0, "rows_repaired": 0, "failed": [], "backfilled": []}
    for ticker in tickers:
        start = min(filter(None, (last.get(ticker), provisional.get(ticker)))) if ticker in last else target
        if ticker in first and first[ticker] > target + timedelta(days=GAP_TOLERANCE_DAYS):
            start = target
            summary["backfilled"].append(ticker)
        try:
            closes = fetcher(ticker, start)
        except Exception as exc:  # network/HTTP/JSON: log and continue with the next ticker
            logger.warning("stock prices for %s failed: %s", ticker, exc)
            summary["failed"].append(ticker)
            continue
        known = {r.price_date: r for r in session.scalars(select(StockPrice).where(StockPrice.ticker == ticker, StockPrice.price_date >= start))}
        for d, c in closes.items():
            if d < start or d > final_until:
                continue  # today's price before the close is no close yet
            row = known.get(d)
            if row is None:
                session.add(StockPrice(ticker=ticker, price_date=d, close=c, fetched_at=now))
                summary["rows_added"] += 1
            elif not is_final_close(row.price_date, row.fetched_at):
                row.close, row.fetched_at = c, now
                summary["rows_repaired"] += 1
        session.commit()
    logger.info("stock price refresh: %d tickers, %d new rows, %d provisional rows repaired, backfill=%s (%d tickers "
                "back to %s), failed: %s", len(tickers), summary["rows_added"], summary["rows_repaired"], backfill,
                len(summary["backfilled"]), target, summary["failed"])
    return summary


def price_status(session: Session) -> dict:
    count, first, last, tickers = session.execute(select(
        func.count(StockPrice.id), func.min(StockPrice.price_date), func.max(StockPrice.price_date),
        func.count(func.distinct(StockPrice.ticker)))).one()
    return {"rows": count, "tickers": tickers, "universe": len(TOP_US_STOCKS),
            "first_date": first.isoformat() if first else None, "last_date": last.isoformat() if last else None}


def main() -> None:
    from backend.app.db import make_engine, make_session_factory

    parser = argparse.ArgumentParser(description="Load daily closes of the top-100 US stocks")
    parser.add_argument("--tickers", nargs="*", help="subset of tickers (default: whole universe)")
    parser.add_argument("--backfill", action="store_true", help=f"also load older closes back to {HISTORY_DAYS} days")
    args = parser.parse_args()
    configure_logging()
    with make_session_factory(make_engine())() as session:
        print(refresh_prices(session, tickers=args.tickers, backfill=args.backfill))


if __name__ == "__main__":
    main()
