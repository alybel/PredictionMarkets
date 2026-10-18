"""Catalog of the stored stock closes: one entry per ticker of the universe with period, returns and quality."""

import logging
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.catalog.grading import MIN_DAYS, assess
from backend.app.models import utcnow
from backend.app.quality.checks import MAX_STOCK_GAP_DAYS, STALE_DAYS, STOCK_JUMP
from backend.app.quality.pit import price_violation
from backend.app.stocks.models import StockPrice
from backend.app.stocks.universe import TOP_US_STOCKS

logger = logging.getLogger(__name__)

MONTH_DAYS = 21  # trading days of one month for the short-term return


def stock_entry(ticker: str, rows: list[tuple[date, float, object]], today: date) -> dict:
    """``rows``: (day, close, fetched_at) oldest first."""
    final = [(d, c) for d, c, f in rows if c is not None and c > 0 and price_violation(d, f, today) is None]
    gaps = sum(1 for (a, _), (b, _) in zip(final, final[1:]) if (b - a).days > MAX_STOCK_GAP_DAYS)
    jumps = sum(1 for (_, a), (_, b) in zip(final, final[1:]) if abs(b / a - 1) >= STOCK_JUMP)
    issues = []
    if not final:
        issues.append("keine Kurse")
    elif len(final) < MIN_DAYS:
        issues.append(f"kurze Historie (< {MIN_DAYS} Tage)")
    if len(final) < len(rows):
        issues.append(f"{len(rows) - len(final)} ungültige oder vorläufige Kurse")
    if gaps:
        issues.append(f"{gaps} Kurslücken > {MAX_STOCK_GAP_DAYS} Tage")
    if jumps:
        issues.append(f"{jumps} Tagesrenditen ≥ {int(STOCK_JUMP * 100)} %")
    if final and (today - final[-1][0]).days > STALE_DAYS:
        issues.append("veraltet")
    ret = lambda n: final[-1][1] / final[-1 - n][1] - 1 if len(final) > n else None  # noqa: E731
    return {"ticker": ticker, "name": TOP_US_STOCKS.get(ticker, ticker), "days": len(final),
            "first_date": final[0][0].isoformat() if final else None, "last_date": final[-1][0].isoformat() if final else None,
            "last_close": final[-1][1] if final else None, "return_1m": ret(MONTH_DAYS),
            "return_total": final[-1][1] / final[0][1] - 1 if len(final) > 1 else None, "gaps": gaps, "jumps": jumps,
            **assess(issues)}


def _rows(session: Session, ticker: str | None = None) -> dict[str, list]:
    stmt = select(StockPrice.ticker, StockPrice.price_date, StockPrice.close, StockPrice.fetched_at).order_by(StockPrice.price_date)
    if ticker:
        stmt = stmt.where(StockPrice.ticker == ticker)
    out: dict[str, list] = {}
    for t, d, c, f in session.execute(stmt):
        out.setdefault(t, []).append((d, c, f))
    return out


def list_stocks(session: Session, today: date | None = None) -> list[dict]:
    """Every ticker of the universe (also those without prices) plus stored tickers outside it."""
    today = today or utcnow().date()
    rows = _rows(session)
    out = [stock_entry(t, rows.get(t, []), today) for t in sorted(set(TOP_US_STOCKS) | set(rows))]
    logger.info("catalog stocks: %d tickers, %d with prices", len(out), sum(1 for s in out if s["days"]))
    return out


def stock_detail(session: Session, ticker: str, today: date | None = None) -> dict | None:
    """Every stored close of one ticker with its daily return and point-in-time verdict; None if nothing is stored."""
    today = today or utcnow().date()
    rows = _rows(session, ticker).get(ticker, [])
    if not rows:
        return None
    out, prev = [], None
    for d, c, f in rows:
        violation = price_violation(d, f, today)
        ok = violation is None and c is not None and c > 0
        out.append({"date": d.isoformat(), "close": c, "return": c / prev - 1 if ok and prev else None, "violation": violation})
        prev = c if ok else prev
    logger.info("catalog stock %s: %d closes", ticker, len(out))
    return {"summary": stock_entry(ticker, rows, today), "rows": out}
