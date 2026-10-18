"""Crash hit rate: does a high (or, if training says so, low) index level precede a fall of more than 10 % within
30 days (risk index 6/7)?

Crash flag of trading day T: min(close in (T, T+30 days]) < 0.9 × close(T). Only days whose window is fully observed
(a close on or after T+30, no gap > MAX_GAP_DAYS inside) get a flag. Walk-forward as in ``walkforward``: per test block
the danger side (mean level on crash vs. other days, default high) and the signal threshold (quantile SIGNAL_QUANTILE
of the training levels) come only from observations whose window ended before the block. Test days overlap by about
OVERLAP trading days, so the binomial test uses hits and signals divided by OVERLAP (independent windows).
"""

from datetime import date, timedelta
from itertools import takewhile

from backend.app.forecast.walkforward import Obs, folds, runs
from backend.app.stocks.stats import binomial_sf

CRASH_DROP = 0.10
CRASH_WINDOW_DAYS = 30
SIGNAL_QUANTILE = 0.8
OVERLAP = 21          # trading days in 30 calendar days
MIN_SIGNALS = 15      # fewer test signals: no verdict


def crash_flags(closes: dict[date, float]) -> dict[date, tuple[date, bool]]:
    """{T: (window end, crash?)} for every trading day with a fully observed window."""
    out = {}
    for run in runs(sorted(closes)):
        for i, day in enumerate(run):
            limit = day + timedelta(CRASH_WINDOW_DAYS)
            if run[-1] < limit or closes[day] <= 0:
                continue  # window not complete (end of data or a gap behind it)
            window = list(takewhile(lambda d: d <= limit, run[i + 1:i + 1 + CRASH_WINDOW_DAYS]))
            out[day] = (window[-1], min(closes[d] for d in window) < closes[day] * (1 - CRASH_DROP))
    return out


def quantile(values: list[float], q: float) -> float:
    s = sorted(values)
    return s[min(len(s) - 1, int(q * len(s)))]


def crash_observations(levels: dict[date, float], closes: dict[date, float]) -> list[Obs]:
    return [Obs(day, end, levels[day], 1.0 if crash else 0.0)
            for day, (end, crash) in sorted(crash_flags(closes).items()) if day in levels]


def danger_side(train: list[Obs]) -> int:
    crash = [o.x for o in train if o.y]
    calm = [o.x for o in train if not o.y]
    return -1 if crash and calm and sum(crash) / len(crash) < sum(calm) / len(calm) else 1


def evaluate_crash(obs: list[Obs]) -> dict:
    days = crashes = signals = hits = 0
    blocks = []
    for train, block in folds(obs):
        if not train:
            continue
        side = danger_side(train)
        threshold = quantile([o.x for o in train], SIGNAL_QUANTILE if side > 0 else 1 - SIGNAL_QUANTILE)
        fired = [o for o in block if side * (o.x - threshold) >= 0]
        days, crashes = days + len(block), crashes + sum(1 for o in block if o.y)
        signals, hits = signals + len(fired), hits + sum(1 for o in fired if o.y)
        blocks.append({"train_to": train[-1].end.isoformat(), "test_from": block[0].day.isoformat(),
                       "side": "hoch" if side > 0 else "niedrig", "threshold": threshold, "signals": len(fired)})
    base = crashes / days if days else None
    rate = hits / signals if signals else None
    out = {"test_days": days, "crash_days": crashes, "signals": signals, "hits": hits, "hit_rate": rate, "base_rate": base,
           "lift": rate / base if rate is not None and base else None, "recall": hits / crashes if crashes else None,
           "p_value": None, "enough": signals >= MIN_SIGNALS and bool(crashes), "folds": blocks,
           "drop": CRASH_DROP, "window_days": CRASH_WINDOW_DAYS}
    if out["enough"]:
        out["p_value"] = binomial_sf(round(hits / OVERLAP), max(1, round(signals / OVERLAP)), base)
    return out
