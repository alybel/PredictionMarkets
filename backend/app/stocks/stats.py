"""Small pure-Python statistics (no numpy in this project): Pearson correlation with its two-sided p-value,
Student's t and F tail probabilities, one-sided binomial test."""

import math

EPSILON = 1e-9  # float noise: a value of exactly the threshold does not exceed it


def mean(values) -> float:
    return sum(values) / len(values)


def pearson(xs: list[float], ys: list[float]) -> tuple[float | None, float | None]:
    """Correlation and OLS slope of ys on xs; (None, None) if a series has no variance or n < 3."""
    n = len(xs)
    if n < 3:
        return None, None
    mx, my = mean(xs), mean(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx == 0 or syy == 0:
        return None, None
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    return sxy / math.sqrt(sxx * syy), sxy / sxx


def correlation_p_value(r: float, n: int) -> float | None:
    """Two-sided p-value of H0 "no correlation" via Student's t with n-2 degrees of freedom."""
    if n < 3:
        return None
    if abs(r) >= 1:
        return 0.0
    df = n - 2
    t2 = r * r * df / (1 - r * r)
    return _betainc(df / 2, 0.5, df / (df + t2))


def t_p_value(t: float, df: int) -> float | None:
    """Two-sided p-value of a t statistic with ``df`` degrees of freedom."""
    if df < 1:
        return None
    return 0.0 if math.isinf(t) else _betainc(df / 2, 0.5, df / (df + t * t))


def f_p_value(f: float, d1: int, d2: int) -> float | None:
    """Upper tail P(F > f) of the F distribution with (d1, d2) degrees of freedom."""
    if d1 < 1 or d2 < 1:
        return None
    if f <= 0:
        return 1.0
    return 0.0 if math.isinf(f) else _betainc(d2 / 2, d1 / 2, d2 / (d2 + d1 * f))


def binomial_sf(k: int, n: int, p: float) -> float:
    """One-sided p-value P(X >= k) for X ~ Binomial(n, p)."""
    if k <= 0:
        return 1.0
    if k > n or p <= 0:
        return 0.0
    if p >= 1:
        return 1.0
    log_p, log_q = math.log(p), math.log(1 - p)
    total = sum(math.exp(math.lgamma(n + 1) - math.lgamma(i + 1) - math.lgamma(n - i + 1) + i * log_p + (n - i) * log_q)
                for i in range(k, n + 1))
    return min(total, 1.0)


def _betainc(a: float, b: float, x: float) -> float:
    """Regularized incomplete beta I_x(a, b) (continued fraction, Numerical Recipes 6.4)."""
    if x <= 0:
        return 0.0
    if x >= 1:
        return 1.0
    front = math.exp(math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b) + a * math.log(x) + b * math.log(1 - x))
    if x > (a + 1) / (a + b + 2):
        return 1.0 - _betainc(b, a, 1 - x)
    return front * _betacf(a, b, x) / a


def _betacf(a: float, b: float, x: float, eps: float = 1e-12, max_iter: int = 300) -> float:
    tiny = 1e-300
    c, d = 1.0, 1.0 - (a + b) * x / (a + 1)
    d = 1.0 / (d if abs(d) > tiny else tiny)
    h = d
    for m in range(1, max_iter + 1):
        for num in (m * (b - m) * x / ((a + 2 * m - 1) * (a + 2 * m)),
                    -(a + m) * (a + b + m) * x / ((a + 2 * m) * (a + 2 * m + 1))):
            d = 1.0 + num * d
            d = 1.0 / (d if abs(d) > tiny else tiny)
            c = 1.0 + num / c
            c = c if abs(c) > tiny else tiny
            h *= d * c
        if abs(d * c - 1.0) < eps:
            break
    return h
