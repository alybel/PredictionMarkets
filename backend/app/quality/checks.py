"""Data quality report of the stored history: coverage per day, look-ahead guards and cleanliness checks.

Every check returns {key, label, status, count, detail}; status is ``ok``, ``blocked`` (rows exist that
the point-in-time guard keeps out of every analysis), ``warn`` (data issue worth a look) or ``info``.
Aggregates run in SQL; snapshot rows are grouped by (day, origin, fetch moment), which stays small
because a live run or a backfill stores all its rows with one ``fetched_at``.
"""

import logging
from collections import Counter
from datetime import date

from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.orm import Session

from backend.app.eui.models import MarketSnapshot, SnapshotRun
from backend.app.models import MIN_LIQUIDITY_USD, utcnow
from backend.app.quality.pit import (AFTER_CLOSE, BACKFILL, FUTURE_DATE, OUTSIDE_DAY, PROVISIONAL_CLOSE,
                                     US_CLOSE_CUTOFF_UTC, FINAL_CLOSE_UTC, price_violation, snapshot_violation)
from backend.app.stocks.models import StockPrice
from backend.app.stocks.universe import TOP_US_STOCKS

logger = logging.getLogger(__name__)

DEFAULT_DAYS = 90
JUMP = 0.5             # day-over-day price change (0..1 scale) that looks like a data error
STOCK_JUMP = 0.25      # day-over-day stock return (25 %) that hints at an unadjusted split
MAX_STOCK_GAP_DAYS = 5  # same gap rule as the correlation
STALE_DAYS = 5
EXAMPLES = 20


def _check(key: str, label: str, count: int, bad_status: str, detail: str, examples: list | None = None) -> dict:
    return {"key": key, "label": label, "count": count, "status": bad_status if count else "ok", "detail": detail,
            "examples": examples or []}


def _is_backfill():
    return func.coalesce(MarketSnapshot.exclusion_reason, "") == BACKFILL


def snapshot_groups(session: Session, today: date) -> list[dict]:
    """Rows per (day, origin, fetch moment) with their point-in-time verdict."""
    backfill = _is_backfill()
    stmt = (select(MarketSnapshot.snapshot_date, backfill, MarketSnapshot.fetched_at, func.count())
            .group_by(MarketSnapshot.snapshot_date, backfill, MarketSnapshot.fetched_at))
    return [{"day": day, "backfill": bool(is_bf), "rows": n,
             "violation": snapshot_violation(day, fetched, BACKFILL if is_bf else None, today)}
            for day, is_bf, fetched, n in session.execute(stmt)]


def day_coverage(groups: list[dict], closes_per_day: dict[date, int], days: int) -> list[dict]:
    """Newest ``days`` snapshot days: live / backfilled / blocked rows and the number of final stock closes."""
    out: dict[date, dict] = {}
    for g in groups:
        row = out.setdefault(g["day"], {"date": g["day"].isoformat(), "live_rows": 0, "backfill_rows": 0, "blocked_rows": 0})
        row["backfill_rows" if g["backfill"] else "live_rows"] += g["rows"]
        row["blocked_rows"] += g["rows"] if g["violation"] else 0
    for day, row in out.items():
        row["stock_closes"] = closes_per_day.get(day, 0)
    return [out[d] for d in sorted(out, reverse=True)[:days]]


def calendar_gaps(dates: list[date]) -> list[dict]:
    """Missing calendar days between consecutive snapshot days."""
    return [{"after": a.isoformat(), "before": b.isoformat(), "missing_days": (b - a).days - 1}
            for a, b in zip(dates, dates[1:]) if (b - a).days > 1]


def _price_jumps(session: Session) -> tuple[int, list[dict]]:
    """Only rows above the global liquidity floor: smaller markets never enter an index (and keeps the scan small)."""
    prev = func.lag(MarketSnapshot.price).over(partition_by=(MarketSnapshot.source, MarketSnapshot.external_id),
                                                order_by=MarketSnapshot.snapshot_date)
    inner = select(MarketSnapshot.source, MarketSnapshot.external_id, MarketSnapshot.title, MarketSnapshot.snapshot_date,
                   MarketSnapshot.price, prev.label("prev")).where(MarketSnapshot.volume_24h_usd >= MIN_LIQUIDITY_USD).subquery()
    jump = func.abs(inner.c.price - inner.c.prev)
    cond = and_(inner.c.prev.is_not(None), inner.c.price.is_not(None), jump >= JUMP)
    count = session.scalar(select(func.count()).select_from(inner).where(cond)) or 0
    rows = session.execute(select(inner).where(cond).order_by(inner.c.snapshot_date.desc()).limit(EXAMPLES))
    return count, [{"source": r.source, "external_id": r.external_id, "title": r.title, "date": r.snapshot_date.isoformat(),
                    "from": r.prev, "to": r.price} for r in rows]


def market_checks(session: Session) -> list[dict]:
    m = MarketSnapshot
    counts = session.execute(select(
        func.sum(case((or_(m.price < 0, m.price > 1), 1), else_=0)),
        func.sum(case((m.price.is_(None), 1), else_=0)),
        func.sum(case((or_(m.volume_24h_usd < 0, m.depth_usd < 0), 1), else_=0)),
        func.sum(case((and_(m.best_bid.is_not(None), m.best_ask.is_not(None), m.best_bid > m.best_ask), 1), else_=0)),
    )).one()
    out_of_range, missing, negative, crossed = (int(c or 0) for c in counts)
    jumps, jump_examples = _price_jumps(session)
    return [
        _check("price_range", "Preis außerhalb 0–1", out_of_range, "warn", "Wahrscheinlichkeiten liegen immer zwischen 0 und 1."),
        _check("price_missing", "Preis fehlt", missing, "info", "Ohne Preis zählt der Markt an diesem Tag nicht zum Index."),
        _check("negative_volume", "Negatives Volumen oder Orderbuch-Tiefe", negative, "warn", "Volumen und Tiefe sind nie negativ."),
        _check("crossed_book", "Geldkurs über Briefkurs", crossed, "warn", "Bid > Ask deutet auf einen fehlerhaften Abruf hin."),
        _check("price_jump", f"Preissprung ≥ {int(JUMP * 100)} Punkte zum Vortag (Märkte ab 10.000 USD Umsatz)", jumps, "warn",
               "Kann echt sein (Entscheidung gefallen), ist aber oft ein Datenfehler – Beispiele prüfen.", jump_examples),
    ]


def run_checks(session: Session, today: date) -> list[dict]:
    bad = list(session.scalars(select(SnapshotRun).where(or_(SnapshotRun.status.in_(("failed", "partial")), and_(
        SnapshotRun.status == "running", SnapshotRun.snapshot_date < today))).order_by(SnapshotRun.id.desc())))
    examples = [{"run_id": r.id, "date": r.snapshot_date.isoformat(), "status": r.status, "note": r.note} for r in bad[:EXAMPLES]]
    return [_check("runs", "Fehlgeschlagene oder unvollständige Tagesläufe", len(bad), "warn",
                   "Teilweise Läufe haben eine Quelle verloren; hängende Läufe wurden nie abgeschlossen.", examples)]


def _price_rows(session: Session) -> dict[str, list[tuple[date, float, object]]]:
    out: dict[str, list] = {}
    for ticker, day, close, fetched in session.execute(
            select(StockPrice.ticker, StockPrice.price_date, StockPrice.close, StockPrice.fetched_at).order_by(StockPrice.price_date)):
        out.setdefault(ticker, []).append((day, close, fetched))
    return out


def stock_checks(prices: dict[str, list], today: date) -> tuple[list[dict], dict, dict[date, int]]:
    """Look-ahead and cleanliness checks of the stock closes, an overview and the final closes per day."""
    verdicts: Counter = Counter()
    gaps, jumps, non_positive, stale, per_day = [], [], [], [], Counter()
    for ticker, rows in prices.items():
        final = []
        for day, close, fetched in rows:
            violation = price_violation(day, fetched, today)
            verdicts[violation] += 1
            if close is None or close <= 0:
                non_positive.append({"ticker": ticker, "date": day.isoformat(), "close": close})
            elif violation is None:
                final.append((day, close))
                per_day[day] += 1
        for (a, ca), (b, cb) in zip(final, final[1:]):
            if (b - a).days > MAX_STOCK_GAP_DAYS:
                gaps.append({"ticker": ticker, "after": a.isoformat(), "before": b.isoformat(), "missing_days": (b - a).days - 1})
            if abs(cb / ca - 1) >= STOCK_JUMP:
                jumps.append({"ticker": ticker, "date": b.isoformat(), "return": cb / ca - 1})
        if final and (today - final[-1][0]).days > STALE_DAYS:
            stale.append({"ticker": ticker, "last_date": final[-1][0].isoformat()})
    missing = sorted(set(TOP_US_STOCKS) - set(prices))
    checks = [
        _check("stock_missing", "Aktien ohne Kurse", len(missing), "warn", "Aktien des Universums ohne gespeicherten Schlusskurs.",
               [{"ticker": t} for t in missing[:EXAMPLES]]),
        _check("stock_non_positive", "Schlusskurs ≤ 0 oder leer", len(non_positive), "warn", "Ungültige Kurse.", non_positive[:EXAMPLES]),
        _check("stock_gap", f"Kurslücke > {MAX_STOCK_GAP_DAYS} Tage", len(gaps), "warn",
               "Über solche Lücken rechnet die Korrelation keine Rendite.", gaps[:EXAMPLES]),
        _check("stock_jump", f"Tagesrendite ≥ {int(STOCK_JUMP * 100)} %", len(jumps), "warn",
               "Hinweis auf einen nicht bereinigten Split oder Abruffehler.", jumps[:EXAMPLES]),
        _check("stock_stale", f"Letzter Kurs älter als {STALE_DAYS} Tage", len(stale), "warn",
               "Der tägliche Kursabruf hat diese Aktien zuletzt nicht geladen.", stale[:EXAMPLES]),
    ]
    overview = {"rows": sum(verdicts.values()), "tickers": len(prices), "universe": len(TOP_US_STOCKS),
                "provisional": verdicts[PROVISIONAL_CLOSE], "future": verdicts[FUTURE_DATE]}
    return checks, overview, dict(per_day)


def lookahead_checks(groups: list[dict], stocks: dict) -> list[dict]:
    blocked = Counter()
    for g in groups:
        blocked[g["violation"]] += g["rows"]
    backfilled = sum(g["rows"] for g in groups if g["backfill"])
    cutoff = US_CLOSE_CUTOFF_UTC.strftime("%H:%M")
    return [
        _check("outside_day", "Marktdaten außerhalb ihres Tages beobachtet", blocked[OUTSIDE_DAY], "blocked",
               "Ein Tageswert darf nur Beobachtungen genau dieses Tages (UTC) enthalten – nie von davor oder danach. "
               "Solche Zeilen fließen in keine Auswertung ein."),
        _check("after_close", f"Marktdaten nach US-Börsenschluss ({cutoff} UTC) beobachtet", blocked[AFTER_CLOSE], "blocked",
               "Der Indexwert von Tag T wird mit der Aktienrendite Schluss(T) → Schluss(T+1) verglichen; was nach dem "
               "Schluss von T gesehen wurde, wäre Zukunftswissen. Gesperrt."),
        _check("future_snapshot", "Marktdaten mit Datum in der Zukunft", blocked[FUTURE_DATE], "blocked",
               "Zeilen mit Datum nach heute werden ignoriert."),
        _check("provisional_close", "Vorläufige Aktienkurse (vor Börsenschluss geladen)", stocks["provisional"], "blocked",
               f"Ein Schlusskurs gilt erst ab {FINAL_CLOSE_UTC.strftime('%H:%M')} UTC als endgültig. Vorläufige Kurse sind "
               "gesperrt und werden beim nächsten Kursabruf überschrieben."),
        _check("future_close", "Aktienkurse mit Datum in der Zukunft", stocks["future"], "blocked", "Werden ignoriert."),
        {"key": "backfill", "label": "Rückgefüllte Tagesstände", "count": backfilled, "status": "info", "examples": [],
         "detail": "Aus den Preisverläufen der Quellen. Jeder Punkt zählt nur für den Tag, an dem er beobachtet wurde, "
                   f"und nur vor {cutoff} UTC (Polymarket ≈ 00:00, Kalshi 04:00 UTC); spätere Punkte werden beim Laden verworfen."},
        {"key": "signal_timing", "label": "Signal-Timing der Korrelation", "count": 0, "status": "info", "examples": [],
         "detail": "Indexänderung von Tag T (vor Börsenschluss bekannt) → Aktienrendite Schluss(T) → Schluss(nächster "
                   "Handelstag). Die Richtung der Signal-Precision wird nur aus der ersten Hälfte der Tage gelernt und "
                   "nur auf der zweiten gemessen."},
    ]


def quality_report(session: Session, days: int = DEFAULT_DAYS, today: date | None = None) -> dict:
    today = today or utcnow().date()
    groups = snapshot_groups(session, today)
    dates = sorted({g["day"] for g in groups})
    stock_list, stocks, closes_per_day = stock_checks(_price_rows(session), today)
    live_days = {g["day"] for g in groups if not g["backfill"]}
    lookahead = lookahead_checks(groups, stocks)
    blocked = sum(c["count"] for c in lookahead if c["status"] == "blocked")
    gaps = calendar_gaps(dates)
    cleanliness = run_checks(session, today) + market_checks(session) + [
        _check("calendar_gap", "Fehlende Snapshot-Tage", sum(g["missing_days"] for g in gaps), "warn",
               "Kalendertage ohne gespeicherten Tagesstand zwischen erstem und letztem Tag.", gaps[-EXAMPLES:])] + stock_list
    logger.info("quality report: %d snapshot days (%d live), %d rows blocked by the point-in-time guard, %d warnings",
                len(dates), len(live_days), blocked, sum(1 for c in cleanliness if c["status"] == "warn"))
    return {
        "generated_at": utcnow().isoformat(), "today": today.isoformat(),
        "snapshots": {"first_date": dates[0].isoformat() if dates else None, "last_date": dates[-1].isoformat() if dates else None,
                      "days": len(dates), "live_days": len(live_days), "rows": sum(g["rows"] for g in groups),
                      "backfill_rows": sum(g["rows"] for g in groups if g["backfill"]), "blocked_rows": sum(
                          g["rows"] for g in groups if g["violation"])},
        "stocks": stocks, "lookahead": lookahead, "cleanliness": cleanliness,
        "coverage": day_coverage(groups, closes_per_day, days),
    }
