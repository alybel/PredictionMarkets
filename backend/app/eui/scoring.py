"""Uncertainty contribution, weights and the 0-100 index value.

Contribution per market: symmetric markets use 1 - 2*|p - 0.5| (1 = total
disagreement, 0 = consensus); directional markets use the price itself,
oriented so that 1 means high uncertainty. Weights follow the square root
of the liquidity score, capped at 10 % per constituent with the excess
redistributed, and the value is 100 * sum(weight * contribution).
"""

import math

from backend.app.eui.models import POLARITY_BAD_IF_NO, POLARITY_BAD_IF_YES, POLARITY_SYMMETRIC

WEIGHT_CAP = 0.10
SCALE = 100.0


def contribution(price: float, polarity: str) -> float:
    """Uncertainty contribution in [0, 1] for a price in [0, 1]."""
    price = min(max(price, 0.0), 1.0)
    if polarity == POLARITY_BAD_IF_YES:
        return price
    if polarity == POLARITY_BAD_IF_NO:
        return 1.0 - price
    if polarity != POLARITY_SYMMETRIC:
        raise ValueError(f"unknown polarity {polarity!r}")
    return 1.0 - 2.0 * abs(price - 0.5)


def sqrt_weights(scores: list[float]) -> list[float]:
    """Normalized square-root weights; all-zero scores give equal weights."""
    roots = [math.sqrt(max(s, 0.0)) for s in scores]
    total = sum(roots)
    if total <= 0:
        return [1.0 / len(scores)] * len(scores) if scores else []
    return [r / total for r in roots]


def cap_weights(weights: list[float], cap: float = WEIGHT_CAP) -> list[float]:
    """Cap each weight and redistribute the excess proportionally among the uncapped ones."""
    n = len(weights)
    if n == 0 or n * cap < 1.0 - 1e-12:
        return list(weights)  # fewer than 1/cap constituents: the cap cannot be honoured
    weights = list(weights)
    capped: set[int] = set()
    for _ in range(n):
        over = [i for i in range(n) if i not in capped and weights[i] > cap + 1e-12]
        if not over:
            break
        excess = sum(weights[i] - cap for i in over)
        for i in over:
            weights[i] = cap
        capped.update(over)
        free = [i for i in range(n) if i not in capped]
        free_total = sum(weights[i] for i in free)
        for i in free:
            weights[i] += excess * (weights[i] / free_total if free_total > 0 else 1.0 / len(free))
    return weights


def weights_from_scores(scores: list[float], cap: float = WEIGHT_CAP) -> list[float]:
    return cap_weights(sqrt_weights(scores), cap)


def renormalize(weights: list[float], cap: float = WEIGHT_CAP) -> list[float]:
    """Rescale stored weights to sum to 1 after constituents dropped out, re-applying the cap."""
    total = sum(weights)
    if total <= 0:
        return list(weights)
    return cap_weights([w / total for w in weights], cap)


def index_value(weights: list[float], contributions: list[float]) -> float | None:
    """0-100 value; None without constituents."""
    if not weights:
        return None
    total = sum(weights)
    if total <= 0:
        return None
    return SCALE * sum(w * c for w, c in zip(weights, contributions)) / total
