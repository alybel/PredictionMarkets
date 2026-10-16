"""Stock evaluation of a metric index (1015-2231): does the change on day T predict the next-day return?

Per stock (and for the market as a top row, always with raw return) two regressions side by side:
* simple:   return = a + b · Δindex (z-points)              -> b, p-value, R² (plus the archive table figures)
* multiple: return = a + Σ b_k · Δmetric_k (raw unit)       -> b_k with p-value each, F-test, R²
Target: excess return over the equal-weighted top-100 market (default) or raw return. Multiple testing: expected
chance hits and Bonferroni threshold for the correlation, the F-test and the single coefficients. A multicollinearity
check of the regressors (pairwise correlation, VIF) warns when single coefficients are unreliable.
The archive index (``correlation``) stays unchanged; this replaces its market attribution for metric indices.
"""

import logging
import math
from datetime import date

from sqlalchemy.orm import Session

from backend.app.builder.models import MetricIndex
from backend.app.models import utcnow
from backend.app.stocks.correlation import ALPHA, MIN_OBSERVATIONS, stock_stats
from backend.app.stocks.metric_data import (EXCESS, RAW, MetricInputs, Universe, load_inputs, load_universe,
                                            observations, target_returns)
from backend.app.stocks.ols import collinearity, ols
from backend.app.stocks.precision import MIN_SIGNALS, robustness_matrix, signal_precision
from backend.app.stocks.trades import trade_series, trade_summary
from backend.app.stocks.universe import TOP_US_STOCKS

logger = logging.getLogger(__name__)

MARKET_TICKER = "MARKT"
MARKET_NAME = "Gesamtmarkt (Ø Top 100, gleichgewichtet)"
ROBUST_Z_CHANGES = (0.25, 0.5, 1.0)  # robustness grid in z-points


def finite(value):
    """JSON-safe copy: ±∞ and NaN become None (e.g. a VIF of a perfectly explained regressor)."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {k: finite(v) for k, v in value.items()}
    if isinstance(value, list):
        return [finite(v) for v in value]
    return value


def multiple_regression(obs: list[dict], components: list[dict]) -> dict:
    """Flat fields of the multiple regression; empty (None) below MIN_OBSERVATIONS or with singular regressors."""
    fit = ols([o["dx"] for o in obs], [o["ret"] for o in obs]) if len(obs) >= MIN_OBSERVATIONS else None
    if fit is None:
        return {"multi_n": len(obs), "multi_r2": None, "multi_adj_r2": None, "multi_f": None, "multi_f_p": None,
                "multi_significant": False, "coefficients": []}
    coefficients = [{"key": c["key"], "coef": b, "std_error": se, "t": t, "p_value": p, "significant": p is not None and p < ALPHA}
                    for c, b, se, t, p in zip(components, fit["coefficients"], fit["std_errors"], fit["t_values"], fit["p_values"])]
    return {"multi_n": fit["n"], "multi_r2": fit["r2"], "multi_adj_r2": fit["adj_r2"], "multi_f": fit["f_stat"],
            "multi_f_p": fit["f_p_value"], "multi_significant": fit["f_p_value"] < ALPHA, "coefficients": coefficients}


def evaluate(obs: list[dict], components: list[dict], min_change: float, min_stock_move: float) -> dict:
    pairs = [(o["dz"], o["ret"]) for o in obs]
    stats = stock_stats(pairs, min_change, points_per_unit=1)  # slope per 1 z-point
    r = stats["correlation"]
    return {**stats, "r2": r * r if r is not None else None, **multiple_regression(obs, components),
            **signal_precision(pairs, min_change, min_stock_move)}


def _flag_multiple_tests(rows: list[dict]) -> dict:
    """Bonferroni flags on the stock rows plus the summary figures (market row excluded)."""
    tested = sum(1 for r in rows if r["correlation"] is not None)
    reg_tested = sum(1 for r in rows if r["multi_f_p"] is not None)
    coef_tests = sum(len(r["coefficients"]) for r in rows)
    signal_tested = sum(1 for r in rows if r["p_signal"] is not None)
    alpha, reg_alpha = (ALPHA / tested if tested else None), (ALPHA / reg_tested if reg_tested else None)
    coef_alpha, signal_alpha = (ALPHA / coef_tests if coef_tests else None), (ALPHA / signal_tested if signal_tested else None)
    for r in rows:
        r["bonferroni"] = r["p_value"] is not None and r["p_value"] < alpha
        r["multi_bonferroni"] = r["multi_f_p"] is not None and r["multi_f_p"] < reg_alpha
        r["signal_significant"] = r["p_signal"] is not None and r["p_signal"] < signal_alpha
        for c in r["coefficients"]:
            c["bonferroni"] = c["p_value"] is not None and c["p_value"] < coef_alpha
    return {
        "stocks_tested": tested, "significant": sum(r["significant"] for r in rows), "alpha": ALPHA,
        "expected_false_positives": round(ALPHA * tested, 1), "bonferroni_alpha": alpha,
        "regression_tested": reg_tested, "regression_significant": sum(r["multi_significant"] for r in rows),
        "regression_expected_false_positives": round(ALPHA * reg_tested, 1), "regression_bonferroni_alpha": reg_alpha,
        "coefficient_tests": coef_tests, "coefficient_significant": sum(c["significant"] for r in rows for c in r["coefficients"]),
        "coefficient_expected_false_positives": round(ALPHA * coef_tests, 1), "coefficient_bonferroni_alpha": coef_alpha,
        "signal_tested": signal_tested, "signal_bonferroni_alpha": signal_alpha,
    }


def _sort_key(row: dict):
    return (row["correlation"] is None, -abs(row["correlation"] or 0), row["ticker"])


def _prepare(session: Session, row: MetricIndex, days: int, today: date | None) -> tuple[MetricInputs, Universe, list[dict], dict]:
    """Inputs, stock universe, market observations (raw return) and the collinearity check of the regressors."""
    inputs = load_inputs(session, row, days, today or utcnow().date())
    universe = load_universe(session, min(inputs.values) if inputs.values else None)
    market_obs = observations(inputs, universe.market_days, universe.market)
    keys = [c["key"] for c in inputs.components]
    return inputs, universe, market_obs, collinearity([o["dx"] for o in market_obs], keys)


def metric_correlation(session: Session, row: MetricIndex, days: int, min_change: float = 0.0, min_stock_move: float = 0.01,
                       target: str = EXCESS, today: date | None = None) -> dict:
    """Table of the metric index against the market (raw return) and the top-100 stocks (``target`` return)."""
    inputs, universe, market_obs, check = _prepare(session, row, days, today)
    market = {"ticker": MARKET_TICKER, "name": MARKET_NAME, "target": RAW,
              **evaluate(market_obs, inputs.components, min_change, min_stock_move)}
    rows = []
    for ticker, name in TOP_US_STOCKS.items():
        obs = observations(inputs, *target_returns(universe, ticker, target))
        rows.append({"ticker": ticker, "name": name, **evaluate(obs, inputs.components, min_change, min_stock_move)})
    rows.sort(key=_sort_key)
    summary = _flag_multiple_tests(rows)
    total_signals = sum(r["signals"] for r in rows)
    logger.info("metric correlation of %r: days=%d target=%s, %d index days, %d market pairs, %d stocks tested, "
                "%d significant (simple), %d significant F-tests, %d significant coefficients, collinearity warning %s "
                "(min_change=%s z, min_stock_move=%s)", row.name, days, target, len(inputs.values), len(market_obs),
                summary["stocks_tested"], summary["significant"], summary["regression_significant"],
                summary["coefficient_significant"], check["warning"], min_change, min_stock_move)
    if check["warning"]:
        logger.warning("metric index %r: strongly correlated components %s, high VIF %s", row.name,
                       [(p["a"], p["b"], round(p["correlation"], 3)) for p in check["strong_pairs"]], check["high_vif"])
    return finite({
        "index_name": row.name, "kind": "metric", "unit": "z", "target": target, "days": days, "min_change": min_change,
        "index_days": len(inputs.values), "future_days_blocked": inputs.future_days, "index_warnings": inputs.warnings,
        "components": inputs.components, "collinearity": check, "market": market,
        "stocks_with_prices": sum(1 for t in TOP_US_STOCKS if universe.closes.get(t)),
        "first_index_date": min(inputs.values).isoformat() if inputs.values else None,
        "first_price_date": min(universe.market_days).isoformat() if universe.market_days else None,
        "min_observations": MIN_OBSERVATIONS, "stocks": rows, **summary,
        "min_stock_move": min_stock_move, "min_signals": MIN_SIGNALS, "total_signals": total_signals,
        "stocks_below_min_signals": sum(1 for r in rows if r["n"] and r["signals"] < MIN_SIGNALS),
    })


def _simple_detail(obs: list[dict], stats: dict) -> dict:
    fit = ols([[o["dz"]] for o in obs], [o["ret"] for o in obs]) if len(obs) >= MIN_OBSERVATIONS else None
    if fit is None:
        return {"n": len(obs), "coef": None, "std_error": None, "t": None, "p_value": None, "intercept": None, "r2": None}
    return {"n": fit["n"], "coef": fit["coefficients"][0], "std_error": fit["std_errors"][0], "t": fit["t_values"][0],
            "p_value": fit["p_values"][0], "intercept": fit["intercept"], "r2": fit["r2"], "correlation": stats["correlation"]}


def metric_stock_detail(session: Session, row: MetricIndex, ticker: str, days: int, min_change: float = 0.0,
                        target: str = EXCESS, today: date | None = None) -> dict:
    """Trades, robustness and both regressions of the metric index against one stock."""
    inputs, universe, _, check = _prepare(session, row, days, today)
    obs = observations(inputs, *target_returns(universe, ticker, target))
    pairs = [(o["dz"], o["ret"]) for o in obs]
    stats = stock_stats(pairs, min_change, points_per_unit=1)
    direction = -1 if (stats["correlation"] or 0) < 0 else 1
    rows = trade_series([(o["day"], o["exit"], o["dz"], o["ret"]) for o in obs], min_change, direction)
    summary = trade_summary(rows)
    multiple = multiple_regression(obs, inputs.components)
    logger.info("metric trades of %r vs %s: days=%d target=%s, %d pairs, %d trades, direction %+d, R² simple %s, "
                "R² multiple %s, F-test p %s", row.name, ticker, days, target, len(obs), summary["trades"], direction,
                stats["correlation"] and round(stats["correlation"] ** 2, 4), multiple["multi_r2"], multiple["multi_f_p"])
    return finite({
        "index_name": row.name, "kind": "metric", "unit": "z", "target": target, "ticker": ticker,
        "name": TOP_US_STOCKS[ticker], "days": days, "min_change": min_change, "direction": direction, "in_sample": True,
        "correlation": stats["correlation"], "p_value": stats["p_value"], "n": stats["n"],
        "summary": summary, "series": rows, "robustness": robustness_matrix(pairs, ROBUST_Z_CHANGES), "min_signals": MIN_SIGNALS,
        "regression": {"simple": _simple_detail(obs, stats), "multiple": multiple, "components": inputs.components,
                       "collinearity": check, "alpha": ALPHA, "min_observations": MIN_OBSERVATIONS},
    })
