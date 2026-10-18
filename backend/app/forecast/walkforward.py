"""Walk-forward forecast power of an index against one target series (risk index 6/7). Pure Python, no I/O.

Grid: trading days of the target that have an index value, split at gaps > MAX_GAP_DAYS. For horizon h (grid steps)
and grid position i: predictor x = I[i] − I[i−h] (index change over the last h steps, known at the close of day i),
outcome y = change of the target from day i to day i+h. A window never spans a gap > MAX_GAP_DAYS.

Walk-forward: observations ordered by day; the first TRAIN_SHARE only trains. The rest is split into FOLDS consecutive
test blocks; block k is predicted with an OLS fit on every observation whose outcome ended before the block's first
day (purged, expanding window). So no test value informs a fit and no fit uses later data. Measured on the pooled
test blocks only: correlation x↔y, forecast correlation ŷ↔y (does the training direction hold?), out-of-sample R²
against the training mean (Campbell/Thompson) and the one-sided p of the forecast correlation with the effective
sample size n/h (overlapping windows are not independent).
"""

import math
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date

from backend.app.forecast.targets import RETURN
from backend.app.stocks.correlation import MAX_GAP_DAYS
from backend.app.stocks.stats import correlation_p_value, mean, pearson

TRAIN_SHARE = 0.5
FOLDS = 3
MIN_TRAIN = 10       # fewer training observations: the fold is skipped
MIN_EFFECTIVE = 10   # fewer effective test observations: "zu wenig Daten"


@dataclass(frozen=True)
class Obs:
    day: date   # T: predictor known at the close of T
    end: date   # last day the outcome depends on
    x: float
    y: float


def runs(days: list[date]) -> list[list[date]]:
    """Sorted days split wherever two neighbours lie more than MAX_GAP_DAYS apart."""
    out: list[list[date]] = []
    for day in days:
        if out and (day - out[-1][-1]).days <= MAX_GAP_DAYS:
            out[-1].append(day)
        else:
            out.append([day])
    return out


def observations(index: dict[date, float], closes: dict[date, float], h: int, kind: str = RETURN,
                 scale: float = 1.0) -> list[Obs]:
    out = []
    for run in runs(sorted(d for d in closes if d in index)):
        for i in range(h, len(run) - h):
            day, end = run[i], run[i + h]
            a, b = closes[day], closes[end]
            if kind == RETURN and a <= 0:
                continue
            y = b / a - 1 if kind == RETURN else (b - a) * scale
            out.append(Obs(day, end, index[day] - index[run[i - h]], y))
    return out


def folds(obs: list[Obs], n_folds: int = FOLDS, train_share: float = TRAIN_SHARE) -> Iterator[tuple[list[Obs], list[Obs]]]:
    """(training, test block) pairs; training = every observation whose outcome ended before the block starts."""
    test = obs[int(len(obs) * train_share):]
    size = math.ceil(len(test) / n_folds) if test else 0
    for k in range(n_folds):
        block = test[k * size:(k + 1) * size]
        if block:
            yield [o for o in obs if o.end < block[0].day], block


def one_sided_p(r: float | None, n: int) -> float | None:
    """P(correlation ≥ r | none) — only a positive forecast correlation counts as forecast power."""
    p = correlation_p_value(r, n) if r is not None and n >= 3 else None
    return None if p is None else (p / 2 if r > 0 else 1 - p / 2)


def evaluate(obs: list[Obs], h: int) -> dict:
    """Out-of-sample statistics of one horizon (see module doc)."""
    xs, ys, preds, bench, blocks = [], [], [], [], []
    for train, block in folds(obs):
        if len(train) < MIN_TRAIN:
            continue
        _, slope = pearson([o.x for o in train], [o.y for o in train])
        if slope is None:
            continue
        mx, my = mean([o.x for o in train]), mean([o.y for o in train])
        xs += [o.x for o in block]
        ys += [o.y for o in block]
        preds += [my + slope * (o.x - mx) for o in block]
        bench += [my] * len(block)
        blocks.append({"train_from": train[0].day.isoformat(), "train_to": train[-1].end.isoformat(), "train_n": len(train),
                       "test_from": block[0].day.isoformat(), "test_to": block[-1].end.isoformat(), "test_n": len(block),
                       "slope": slope})
    n, n_eff = len(ys), len(ys) // h
    out = {"horizon": h, "observations": len(obs), "test_n": n, "effective_n": n_eff, "folds": blocks,
           "enough": n_eff >= MIN_EFFECTIVE, "correlation": None, "forecast_correlation": None, "p_value": None,
           "oos_r2": None, "slope": blocks[-1]["slope"] if blocks else None, "test_slope": None}
    if not out["enough"]:
        return out
    r, test_slope = pearson(xs, ys)
    fc, _ = pearson(preds, ys)
    sst = sum((y - b) ** 2 for y, b in zip(ys, bench))
    out.update(correlation=r, forecast_correlation=fc, p_value=one_sided_p(fc, n_eff), test_slope=test_slope,
               oos_r2=1 - sum((y - p) ** 2 for y, p in zip(ys, preds)) / sst if sst > 0 else None)
    return out
