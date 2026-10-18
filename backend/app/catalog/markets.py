"""Catalog of the stored prediction markets: one entry per market with period, latest state and quality.

The list is aggregated in SQL (one group per source/market) and paged; latest values and price jumps are
read only for the markets of the requested page, so the catalog stays fast with 100 000+ markets.
"""

import logging
from datetime import date

from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.orm import Session

from backend.app.catalog.grading import MIN_COVERAGE, MIN_DAYS, assess, coverage
from backend.app.eui.models import MarketSnapshot
from backend.app.models import MIN_LIQUIDITY_USD
from backend.app.quality.checks import JUMP
from backend.app.quality.explore import market_rows
from backend.app.quality.pit import BACKFILL

logger = logging.getLogger(__name__)

PAGE_LIMIT = 100
SORTS = ("days", "volume", "last_date", "title")


def market_issues(days: int, cover: float | None, missing_price: int, jumps: int, avg_volume: float | None) -> list[str]:
    issues = []
    if days < MIN_DAYS:
        issues.append(f"kurze Historie (< {MIN_DAYS} Tage)")
    if cover is not None and cover < MIN_COVERAGE:
        issues.append(f"Lücken ({round(cover * 100)} % der Tage)")
    if missing_price:
        issues.append(f"{missing_price} Tage ohne Preis")
    if jumps:
        issues.append(f"{jumps} Preissprünge")
    if (avg_volume or 0) < MIN_LIQUIDITY_USD:
        issues.append("geringe Liquidität")
    return issues


def _jumps(prices: list[float | None]) -> int:
    known = [p for p in prices if p is not None]
    return sum(1 for a, b in zip(known, known[1:]) if abs(b - a) >= JUMP)


def latest_date(session: Session) -> date | None:
    return session.scalar(select(func.max(MarketSnapshot.snapshot_date)))


def catalog_summary(session: Session) -> list[dict]:
    """Markets, rows and period per source."""
    m = MarketSnapshot
    per_market = (select(m.source, func.count().label("n"), func.min(m.snapshot_date).label("a"), func.max(m.snapshot_date).label("b"))
                  .group_by(m.source, m.external_id).subquery())
    stmt = (select(per_market.c.source, func.count(), func.sum(per_market.c.n), func.min(per_market.c.a), func.max(per_market.c.b))
            .group_by(per_market.c.source).order_by(per_market.c.source))
    return [{"source": s, "markets": n, "rows": int(r or 0), "first_date": a.isoformat(), "last_date": b.isoformat()}
            for s, n, r, a, b in session.execute(stmt)]


def list_markets(session: Session, *, q: str = "", source: str = "", min_days: int = 1, current: bool = False,
                 sort: str = "days", limit: int = 50, offset: int = 0) -> dict:
    """One page of the catalog plus the total number of matching markets."""
    m = MarketSnapshot
    days, avg_volume = func.count().label("days"), func.avg(m.volume_24h_usd).label("avg_volume")
    stmt = select(m.source, m.external_id, func.max(m.title).label("title"), days, func.min(m.snapshot_date).label("first_date"),
                  func.max(m.snapshot_date).label("last_date"), func.sum(case((m.price.is_(None), 1), else_=0)).label("missing_price"),
                  func.sum(case((m.exclusion_reason == BACKFILL, 1), else_=0)).label("backfill_days"), avg_volume)
    if q.strip():
        stmt = stmt.where(func.lower(m.title).like(f"%{q.strip().lower()}%"))
    if source:
        stmt = stmt.where(m.source == source)
    stmt = stmt.group_by(m.source, m.external_id).having(func.count() >= min_days)
    newest = latest_date(session)
    if current and newest:
        stmt = stmt.having(func.max(m.snapshot_date) == newest)
    total = session.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    order = {"days": (days.desc(),), "volume": (avg_volume.desc(),), "last_date": (func.max(m.snapshot_date).desc(),),
             "title": (func.max(m.title),)}[sort if sort in SORTS else "days"]
    page = session.execute(stmt.order_by(*order, m.source, m.external_id).limit(min(limit, PAGE_LIMIT)).offset(offset)).all()
    items = _with_details(session, page, newest)
    logger.info("catalog markets q=%r source=%r min_days=%d current=%s sort=%s offset=%d: %d of %d",
                q, source, min_days, current, sort, offset, len(items), total)
    return {"total": total, "offset": offset, "latest_date": newest.isoformat() if newest else None, "items": items}


def _with_details(session: Session, page, newest: date | None) -> list[dict]:
    """Latest state and price jumps for the markets of one page (a single query over their rows)."""
    if not page:
        return []
    m = MarketSnapshot
    keys = or_(*(and_(m.source == r.source, m.external_id == r.external_id) for r in page))
    series: dict[tuple, list] = {}
    for row in session.execute(select(m.source, m.external_id, m.price, m.volume_24h_usd, m.url, m.end_date)
                               .where(keys).order_by(m.snapshot_date)):
        series.setdefault((row.source, row.external_id), []).append(row)
    out = []
    for r in page:
        rows = series.get((r.source, r.external_id), [])
        last = rows[-1] if rows else None
        cover = coverage(r.days, r.first_date, r.last_date)
        jumps = _jumps([x.price for x in rows])
        out.append({"source": r.source, "external_id": r.external_id, "title": r.title, "days": r.days,
                    "first_date": r.first_date.isoformat(), "last_date": r.last_date.isoformat(),
                    "current": r.last_date == newest, "backfill_days": int(r.backfill_days or 0),
                    "coverage": cover, "missing_price": int(r.missing_price or 0), "jumps": jumps,
                    "avg_volume_24h_usd": r.avg_volume, "last_price": last.price if last else None,
                    "last_volume_24h_usd": last.volume_24h_usd if last else None, "url": last.url if last else None,
                    "end_date": last.end_date.isoformat() if last and last.end_date else None,
                    **assess(market_issues(r.days, cover, int(r.missing_price or 0), jumps, r.avg_volume))})
    return out


def market_detail(session: Session, source: str, external_id: str, today: date | None = None) -> dict | None:
    """Full history of one market (see quality.explore) plus its catalog entry; None if nothing is stored."""
    rows = market_rows(session, source, external_id, today)
    if not rows:
        return None
    first, last = date.fromisoformat(rows[0]["date"]), date.fromisoformat(rows[-1]["date"])
    cover = coverage(len(rows), first, last)
    missing = sum(1 for r in rows if r["price"] is None)
    jumps = sum(1 for r in rows if r["jump"])
    vols = [r["volume_24h_usd"] for r in rows if r["volume_24h_usd"] is not None]
    avg_volume = sum(vols) / len(vols) if vols else None
    title = session.scalar(select(MarketSnapshot.title).where(MarketSnapshot.source == source, MarketSnapshot.external_id == external_id)
                           .order_by(MarketSnapshot.snapshot_date.desc()).limit(1))
    summary = {"source": source, "external_id": external_id, "title": title, "days": len(rows), "first_date": rows[0]["date"],
               "last_date": rows[-1]["date"], "coverage": cover, "missing_price": missing, "jumps": jumps,
               "blocked": sum(1 for r in rows if r["violation"]), "avg_volume_24h_usd": avg_volume,
               **assess(market_issues(len(rows), cover, missing, jumps, avg_volume))}
    logger.info("catalog market %s/%s: %d days, grade %s", source, external_id, len(rows), summary["grade"])
    return {"summary": summary, "rows": rows}
