"""Does a change in a prediction-market index predict the next-day return of the top-100 US stocks?

Timing (no look-ahead): the daily snapshot of day T is taken before the US close of T, so the index
change ΔI_T = I_T − I_prev is known before the close of T. It is paired with the stock's next-day
return close(T) → close(next trading day). Only snapshot days that are trading days of the stock
enter; I_prev is the previous such day, so weekend/holiday changes are carried into the next session.

Per stock: n, Pearson correlation of ΔI and the next-day return with its p-value (Student's t),
slope (return per index point), hit rate (sign of the return = sign of ΔI) and the average next-day
return after index rises vs. falls. With 100 stocks tested at p < 0.05 about 5 hits are expected by
chance, so the payload also gives that number and the Bonferroni threshold.

On top, each stock gets the out-of-sample signal precision with a stock-move threshold (see ``precision``).
"""

import logging
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.models import IndexRule, utcnow
from backend.app.quality.pit import price_violation
from backend.app.history import DEFAULT_DAYS, index_history
from backend.app.stocks.models import StockPrice
from backend.app.stocks.precision import MIN_SIGNALS, signal_precision
from backend.app.stocks.stats import EPSILON, correlation_p_value, mean, pearson  # EPSILON: a change of exactly min_change is no event
from backend.app.stocks.universe import TOP_US_STOCKS

logger = logging.getLogger(__name__)

ALPHA = 0.05
MIN_OBSERVATIONS = 10  # fewer pairs give no meaningful statistic
DEFAULT_MIN_STOCK_MOVE = 0.01  # next-day return (1 %) a signal must exceed to count as a hit
MAX_GAP_DAYS = 5       # ΔI and returns only across short gaps (weekend + holiday), never across data holes


def closes_by_ticker(session: Session, start: date, only: str | None = None) -> dict[str, dict[date, float]]:
    """Final closes from ``start`` on; provisional (intraday) and future-dated rows never enter (``quality.pit``)."""
    query = select(StockPrice.ticker, StockPrice.price_date, StockPrice.close, StockPrice.fetched_at).where(StockPrice.price_date >= start)
    if only is not None:
        query = query.where(StockPrice.ticker == only)
    out: dict[str, dict[date, float]] = {}
    today, blocked = utcnow().date(), 0
    for ticker, day, close, fetched in session.execute(query):
        if price_violation(day, fetched, today):
            blocked += 1
            continue
        out.setdefault(ticker, {})[day] = close
    if blocked:
        logger.warning("point-in-time guard: %d stock closes blocked (provisional or future-dated)", blocked)
    return out


def next_day_returns(closes: dict[date, float]) -> dict[date, tuple[date, float]]:
    """{T: (next trading day, return close T → close next day)}; never across a gap > MAX_GAP_DAYS."""
    trading_days = sorted(closes)
    return {day: (nxt, closes[nxt] / closes[day] - 1) for day, nxt in zip(trading_days, trading_days[1:])
            if (nxt - day).days <= MAX_GAP_DAYS and closes[day] > 0}


def paired_days(index_days, trading_days, returns: dict[date, tuple[date, float]]) -> list[tuple[date, date, date, float]]:
    """(prev, T, next trading day, return after T) for every trading day T with an index value whose predecessor
    (previous trading day with an index value) lies at most MAX_GAP_DAYS back."""
    days = [d for d in sorted(index_days) if d in trading_days]
    return [(prev, day, *returns[day]) for prev, day in zip(days, days[1:])
            if (day - prev).days <= MAX_GAP_DAYS and day in returns]


def signal_days(index_values: dict[date, float], closes: dict[date, float]) -> list[tuple[date, date, float, float]]:
    """(T, next trading day, ΔI_T, next-day return after T) for every trading day T with an index value and a known predecessor."""
    return [(day, nxt, index_values[day] - index_values[prev], ret)
            for prev, day, nxt, ret in paired_days(index_values, closes, next_day_returns(closes))]


def signal_pairs(index_values: dict[date, float], closes: dict[date, float]) -> list[tuple[float, float]]:
    """(ΔI_T, next-day return after T) — see ``signal_days``."""
    return [(change, ret) for _, _, change, ret in signal_days(index_values, closes)]


def stock_stats(pairs: list[tuple[float, float]], min_change: float = 0.0, points_per_unit: float = 100) -> dict:
    """Statistics of one stock; ``min_change`` (in the index unit) defines an up/down event.

    ``points_per_unit``: index points per unit of ΔI for ``slope_per_point`` (100 for the 0..1 archive index,
    1 for a metric index in z-points)."""
    n = len(pairs)
    r, slope = pearson([p[0] for p in pairs], [p[1] for p in pairs]) if n >= MIN_OBSERVATIONS else (None, None)
    ups = [ret for d, ret in pairs if d > min_change + EPSILON]
    downs = [ret for d, ret in pairs if d < -min_change - EPSILON]
    hits = sum(1 for ret in ups if ret > 0) + sum(1 for ret in downs if ret < 0)
    p_value = correlation_p_value(r, n) if r is not None else None
    return {
        "n": n, "correlation": r, "p_value": p_value, "significant": p_value is not None and p_value < ALPHA,
        "slope_per_point": slope / points_per_unit if slope is not None else None,  # next-day return per 1 index point
        "events_up": len(ups), "events_down": len(downs),
        "hit_rate": hits / (len(ups) + len(downs)) if ups or downs else None,
        "mean_return_after_up": mean(ups) if ups else None,
        "mean_return_after_down": mean(downs) if downs else None,
    }


def _sort_key(row: dict):
    return (row["correlation"] is None, -abs(row["correlation"] or 0), row["ticker"])


def index_values(session: Session, rule: IndexRule, days: int) -> dict[date, float]:
    """Daily index values of the backtest (days without a value are left out)."""
    history = index_history(session, rule, days)
    return {date.fromisoformat(p["date"]): p["value"] for p in history["points"] if p["value"] is not None}


def index_correlation(session: Session, rule: IndexRule, days: int = DEFAULT_DAYS, min_change: float = 0.0,
                      min_stock_move: float = DEFAULT_MIN_STOCK_MOVE) -> dict:
    """Correlation table of one index (stored or unsaved rule) against the whole stock universe."""
    values = index_values(session, rule, days)
    closes = closes_by_ticker(session, min(values)) if values else {}
    rows = []
    for ticker, name in TOP_US_STOCKS.items():
        pairs = signal_pairs(values, closes.get(ticker, {}))
        rows.append({"ticker": ticker, "name": name, **stock_stats(pairs, min_change),
                     **signal_precision(pairs, min_change, min_stock_move)})
    rows.sort(key=_sort_key)
    tested = sum(1 for row in rows if row["correlation"] is not None)
    significant = sum(1 for row in rows if row["significant"])
    signal_tested = sum(1 for row in rows if row["p_signal"] is not None)
    signal_alpha = ALPHA / signal_tested if signal_tested else None
    for row in rows:
        row["signal_significant"] = row["p_signal"] is not None and row["p_signal"] < signal_alpha
    total_signals = sum(row["signals"] for row in rows)
    below_min = sum(1 for row in rows if row["n"] and row["signals"] < MIN_SIGNALS)
    logger.info("correlation of %r: %d index days, %d stocks tested, %d significant (min_change=%s)",
                rule.index_name, len(values), tested, significant, min_change)
    logger.info("signal precision of %r: %d test signals in total, %d stocks below %d signals, %d signal-significant "
                "(min_stock_move=%s)", rule.index_name, total_signals, below_min, MIN_SIGNALS,
                sum(1 for row in rows if row["signal_significant"]), min_stock_move)
    return {
        "index_name": rule.index_name, "days": days, "min_change": min_change, "index_days": len(values),
        "stocks_with_prices": sum(1 for t in TOP_US_STOCKS if closes.get(t)),
        "stocks_tested": tested, "significant": significant, "alpha": ALPHA,
        "expected_false_positives": round(ALPHA * tested, 1),
        "bonferroni_alpha": ALPHA / tested if tested else None,
        "min_observations": MIN_OBSERVATIONS, "stocks": rows,
        "min_stock_move": min_stock_move, "min_signals": MIN_SIGNALS, "signal_tested": signal_tested,
        "signal_bonferroni_alpha": signal_alpha, "total_signals": total_signals, "stocks_below_min_signals": below_min,
    }
