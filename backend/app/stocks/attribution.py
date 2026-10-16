"""Performance attribution per index component for one stock (1015-1200).

The index is I_t = Σ w_i,t · p_i,t with w = 24h-turnover share (see ``aggregation``). Between the two index
days of a signal pair (prev → T, same pairs as the correlation table) the change splits exactly into
  price effect   w_i,prev · (p_i,T − p_i,prev)
  weight effect  (w_i,T − w_i,prev) · p_i,T          (re-weighting incl. entries and exits)
A market entering has no previous price (taken = p_T → price effect 0), an exiting one no current price
(taken = p_prev), so entries and exits land fully in the weight effect and Σ effects = ΔI_T.

Covariance decomposition (as in a risk attribution): cov(ΔI, r) = Σ_i cov(c_i, r), so the correlation and the
beta of the main table split additively into component shares sxy_i / √(sxx·syy) and sxy_i / sxx, each again
into price and weight effect. Whole-period view only; no per-component p-values (few index days → much noise).
"""

import logging
import math
import time
from datetime import date

from sqlalchemy.orm import Session

from backend.app.aggregation import aggregate
from backend.app.history import daily_selection
from backend.app.models import IndexRule
from backend.app.stocks.correlation import MIN_OBSERVATIONS, closes_by_ticker, signal_days, stock_stats
from backend.app.stocks.stats import mean, pearson
from backend.app.stocks.universe import TOP_US_STOCKS

logger = logging.getLogger(__name__)

TOP_COMPONENTS = 20
SHARE_KEYS = ("corr_price", "corr_weight", "corr_total", "beta_price", "beta_weight", "beta_total")


def market_key(market) -> str:
    return f"{market.source}:{market.external_id}"


def component_weights(selected) -> dict[str, tuple[float, float]]:
    """{market key: (weight, price)} exactly as ``aggregate`` weights the priced markets (weight 0 is left out)."""
    priced = [m for m in selected if m.price is not None]
    total = sum(m.liquidity or 0.0 for m in priced)
    weights = {market_key(m): ((m.liquidity or 0.0) / total if total > 0 else 1 / len(priced), m.price) for m in priced}
    return {k: wp for k, wp in weights.items() if wp[0] > 0}


def day_effects(prev: dict[str, tuple[float, float]], cur: dict[str, tuple[float, float]]) -> dict[str, tuple[float, float]]:
    """{market key: (price effect, weight effect)}; their total over all keys is exactly I_cur − I_prev."""
    out = {}
    for key in prev.keys() | cur.keys():
        w0, p0 = prev.get(key, (0.0, None))
        w1, p1 = cur.get(key, (0.0, None))
        p0 = p1 if p0 is None else p0
        p1 = p0 if p1 is None else p1
        out[key] = (w0 * (p1 - p0), (w1 - w0) * p1)
    return out


def _empty_row(key: str, meta: dict) -> dict:
    return {"key": key, **meta.get(key, {"source": None, "title": key, "url": None}), **{k: 0.0 for k in SHARE_KEYS}}


def attribute(values: dict[date, float], weights: dict[date, dict], closes: dict[date, float],
              meta: dict | None = None, top: int = TOP_COMPONENTS) -> dict:
    """Shares of correlation and beta per component; ``values``/``weights`` per index day, ``closes`` of the stock."""
    meta = meta or {}
    signals = signal_days(values, closes)
    stats = stock_stats([(change, ret) for _, _, change, ret in signals])
    result = {"n": stats["n"], "correlation": stats["correlation"], "slope_per_point": stats["slope_per_point"],
              "min_observations": MIN_OBSERVATIONS, "components": [], "others": None, "total": None, "component_count": 0}
    if stats["correlation"] is None:  # fewer than MIN_OBSERVATIONS pairs or no variance — as in the main table
        return result
    trading = [d for d in sorted(values) if d in closes]
    prev_of = dict(zip(trading[1:], trading))
    changes = [change for _, _, change, _ in signals]
    returns = [ret for _, _, _, ret in signals]
    mx, my = mean(changes), mean(returns)
    sxx = sum((x - mx) ** 2 for x in changes)
    syy = sum((y - my) ** 2 for y in returns)
    corr_scale, beta_scale = 1 / math.sqrt(sxx * syy), 1 / sxx / 100  # beta per index point like slope_per_point
    rows: dict[str, dict] = {}
    own: dict[str, list[tuple[float, float]]] = {}
    used_days: set[date] = set()
    for day, _, _, ret in signals:
        prev = prev_of[day]
        used_days |= {prev, day}
        for key, (price_eff, weight_eff) in day_effects(weights[prev], weights[day]).items():
            row = rows.setdefault(key, _empty_row(key, meta))
            dev = ret - my  # sxy_i = Σ c_i,t · (r_t − r̄)
            row["corr_price"] += price_eff * dev * corr_scale
            row["corr_weight"] += weight_eff * dev * corr_scale
            row["beta_price"] += price_eff * dev * beta_scale
            row["beta_weight"] += weight_eff * dev * beta_scale
            own.setdefault(key, []).append((price_eff + weight_eff, ret))
    for key, row in rows.items():
        row["corr_total"] = row["corr_price"] + row["corr_weight"]
        row["beta_total"] = row["beta_price"] + row["beta_weight"]
        held = [weights[d][key][0] for d in sorted(used_days) if key in weights[d]]
        row["days_in_index"], row["avg_weight"] = len(held), mean(held) if held else 0.0
        pairs = own[key]
        row["own_correlation"] = pearson(*zip(*pairs))[0] if len(pairs) >= MIN_OBSERVATIONS else None
        row["own_n"] = len(pairs)
    ranked = sorted(rows.values(), key=lambda r: (-abs(r["corr_total"]), r["key"]))
    rest = ranked[top:]
    result.update(components=ranked[:top], component_count=len(ranked),
                  others={"count": len(rest), **{k: sum(r[k] for r in rest) for k in SHARE_KEYS}} if rest else None,
                  total={"count": len(ranked), **{k: sum(r[k] for r in ranked) for k in SHARE_KEYS}})
    return result


def stock_attribution(session: Session, rule: IndexRule, ticker: str, days: int, min_change: float = 0.0) -> dict:
    """Component attribution of ``rule`` (stored or unsaved) against one stock; DB untouched."""
    started = time.perf_counter()
    values, weights, meta = {}, {}, {}
    for day, selected in daily_selection(session, rule, days):
        value = aggregate(rule.index_name, selected).value  # identical to the backtest value of the main table
        if value is None:
            continue
        values[day], weights[day] = value, component_weights(selected)
        meta.update({market_key(m): {"source": m.source, "title": m.title, "url": m.url} for m in selected})
    closes = closes_by_ticker(session, min(values), ticker).get(ticker, {}) if values else {}
    result = attribute(values, weights, closes, meta)
    logger.info("attribution of %r vs %s: %d index days, %d pairs, %d components, %.0f ms (min_change=%s)",
                rule.index_name, ticker, len(values), result["n"], result["component_count"],
                (time.perf_counter() - started) * 1000, min_change)
    # min_change is echoed: like in the main table it defines up/down events only, never the correlation/beta
    return {"index_name": rule.index_name, "ticker": ticker, "name": TOP_US_STOCKS[ticker], "days": days,
            "min_change": min_change, "index_days": len(values), **result}
