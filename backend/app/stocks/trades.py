"""Trade view of one index/stock pair: the daily signal, the trade it triggers and the resulting performance.

Rule (same timing as the correlation, no look-ahead in the execution): on snapshot day T the index change
ΔI_T is known before the US close; if |ΔI_T| > min_change the stock is bought (or sold short) at the close
of T and closed at the next close. Direction: with a positive correlation an index rise means long, with a
negative one short. That direction comes from the correlation of the whole period, i.e. it is an in-sample
check of the pattern, not an out-of-sample backtest — the payload says so via ``in_sample``.
"""

import logging

from sqlalchemy.orm import Session

from backend.app.models import IndexRule
from backend.app.stocks.correlation import (EPSILON, closes_by_ticker, index_values, signal_days, signal_pairs,
                                            stock_stats)
from backend.app.stocks.precision import MIN_SIGNALS, robustness_matrix
from backend.app.stocks.universe import TOP_US_STOCKS

logger = logging.getLogger(__name__)


def trade_series(days: list[tuple], min_change: float, direction: int) -> list[dict]:
    """One row per signal day with position (+1 long, −1 short, 0 flat) and the cumulative performance."""
    strategy = buy_hold = 1.0
    rows = []
    for day, nxt, change, ret in days:
        position = 0 if abs(change) <= min_change + EPSILON else direction * (1 if change > 0 else -1)
        trade_return = position * ret
        strategy *= 1 + trade_return
        buy_hold *= 1 + ret
        rows.append({
            "date": day.isoformat(), "exit_date": nxt.isoformat(), "index_change": change, "stock_return": ret,
            "position": position, "trade_return": trade_return if position else None,
            "strategy": strategy - 1, "buy_hold": buy_hold - 1,
        })
    return rows


def trade_summary(rows: list[dict]) -> dict:
    trades = [r for r in rows if r["position"]]
    wins = sum(1 for r in trades if r["trade_return"] > 0)
    return {
        "days": len(rows), "trades": len(trades), "wins": wins,
        "longs": sum(1 for r in trades if r["position"] > 0), "shorts": sum(1 for r in trades if r["position"] < 0),
        "win_rate": wins / len(trades) if trades else None,
        "strategy_return": rows[-1]["strategy"] if rows else None,
        "buy_hold_return": rows[-1]["buy_hold"] if rows else None,
    }


def stock_trades(session: Session, rule: IndexRule, ticker: str, days: int, min_change: float = 0.0) -> dict:
    """Signal, trades and performance of ``rule`` against one stock of the universe."""
    values = index_values(session, rule, days)
    closes = closes_by_ticker(session, min(values), ticker).get(ticker, {}) if values else {}
    signals = signal_days(values, closes)
    pairs = signal_pairs(values, closes)
    stats = stock_stats(pairs, min_change)
    direction = -1 if (stats["correlation"] or 0) < 0 else 1
    rows = trade_series(signals, min_change, direction)
    summary = trade_summary(rows)
    logger.info("trades of %r vs %s: %d signal days, %d trades, direction %+d, strategy %s, buy&hold %s",
                rule.index_name, ticker, len(rows), summary["trades"], direction,
                summary["strategy_return"], summary["buy_hold_return"])
    return {
        "index_name": rule.index_name, "ticker": ticker, "name": TOP_US_STOCKS[ticker], "days": days,
        "min_change": min_change, "direction": direction, "in_sample": True,
        "correlation": stats["correlation"], "p_value": stats["p_value"], "n": stats["n"],
        "summary": summary, "series": rows,
        "robustness": robustness_matrix(pairs), "min_signals": MIN_SIGNALS,  # out-of-sample grid (1015-1157)
    }
