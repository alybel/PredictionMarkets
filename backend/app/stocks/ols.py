"""Ordinary least squares with intercept in pure Python (no numpy in this project), plus a multicollinearity check.

``ols`` regresses y on k regressors on centered data (the intercept follows from the means), so (X'X)⁻¹ stays small
(k × k). It returns per regressor coefficient, standard error, t and two-sided p-value, and for the whole model R²,
adjusted R² and the F-test of "all coefficients are 0". ``collinearity`` reports the pairwise correlations and the
variance inflation factor (VIF) of each regressor; strongly related regressors make the single coefficients unstable.
"""

import math

from backend.app.stocks.stats import f_p_value, mean, pearson, t_p_value

SINGULAR = 1e-10      # relative pivot below which X'X counts as singular (perfectly collinear regressors)
VIF_LIMIT = 5.0       # VIF from which the single coefficients are unreliable
CORR_LIMIT = 0.8      # |correlation| of two regressors from which they are "strongly correlated"


def _inverse(matrix: list[list[float]]) -> list[list[float]] | None:
    """Gauss-Jordan inverse with partial pivoting; None if singular."""
    n = len(matrix)
    scale = max(abs(matrix[i][i]) for i in range(n)) or 1.0
    a = [row[:] + [1.0 if i == j else 0.0 for j in range(n)] for i, row in enumerate(matrix)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(a[r][col]))
        if abs(a[pivot][col]) < SINGULAR * scale:
            return None
        a[col], a[pivot] = a[pivot], a[col]
        p = a[col][col]
        a[col] = [v / p for v in a[col]]
        for r in range(n):
            if r != col and a[r][col]:
                f = a[r][col]
                a[r] = [v - f * w for v, w in zip(a[r], a[col])]
    return [row[n:] for row in a]


def ols(xs: list[list[float]], ys: list[float]) -> dict | None:
    """Regression of ``ys`` on the rows of ``xs`` (one row per observation, k columns) with intercept.

    None if there are not more observations than parameters, y has no variance or the regressors are singular."""
    n, k = len(ys), len(xs[0]) if xs else 0
    df = n - k - 1
    if k == 0 or df < 1:
        return None
    means = [mean([row[j] for row in xs]) for j in range(k)]
    my = mean(ys)
    xc = [[row[j] - means[j] for j in range(k)] for row in xs]
    yc = [y - my for y in ys]
    sst = sum(y * y for y in yc)
    if sst == 0:
        return None
    xtx = [[sum(row[i] * row[j] for row in xc) for j in range(k)] for i in range(k)]
    inv = _inverse(xtx)
    if inv is None:
        return None
    xty = [sum(row[i] * y for row, y in zip(xc, yc)) for i in range(k)]
    coef = [sum(inv[i][j] * xty[j] for j in range(k)) for i in range(k)]
    ssr = sum((y - sum(c * x for c, x in zip(coef, row))) ** 2 for row, y in zip(xc, yc))
    r2 = max(0.0, 1 - ssr / sst)
    sigma2 = ssr / df
    errors = [math.sqrt(max(inv[i][i] * sigma2, 0.0)) for i in range(k)]
    ts = [c / e if e > 0 else math.copysign(math.inf, c) if c else 0.0 for c, e in zip(coef, errors)]
    f = (r2 / k) / ((1 - r2) / df) if r2 < 1 else math.inf
    return {
        "n": n, "k": k, "df": df, "intercept": my - sum(c * m for c, m in zip(coef, means)),
        "coefficients": coef, "std_errors": errors, "t_values": ts, "p_values": [t_p_value(t, df) for t in ts],
        "r2": r2, "adj_r2": 1 - (1 - r2) * (n - 1) / df, "f_stat": f, "f_p_value": f_p_value(f, k, df),
    }


def vif(xs: list[list[float]]) -> list[float | None]:
    """Variance inflation factor 1 / (1 − R²_j) of every regressor against all others (∞ if perfectly explained)."""
    k = len(xs[0]) if xs else 0
    if k < 2:
        return [1.0] * k
    out = []
    for j in range(k):
        fit = ols([[v for i, v in enumerate(row) if i != j] for row in xs], [row[j] for row in xs])
        if fit is None:  # regressor without variance or the others singular
            out.append(None if all(row[j] == xs[0][j] for row in xs) else math.inf)
        else:
            out.append(1 / (1 - fit["r2"]) if fit["r2"] < 1 else math.inf)
    return out


def collinearity(xs: list[list[float]], names: list[str]) -> dict:
    """Pairwise correlations and VIF of the regressors; ``warning`` if |r| ≥ CORR_LIMIT or a VIF ≥ VIF_LIMIT."""
    k = len(names)
    pairs = []
    for i in range(k):
        for j in range(i + 1, k):
            r, _ = pearson([row[i] for row in xs], [row[j] for row in xs]) if len(xs) >= 3 else (None, None)
            pairs.append({"a": names[i], "b": names[j], "correlation": r})
    factors = vif(xs) if len(xs) > k + 1 else [None] * k
    strong = [p for p in pairs if p["correlation"] is not None and abs(p["correlation"]) >= CORR_LIMIT]
    high = [names[j] for j, v in enumerate(factors) if v is not None and v >= VIF_LIMIT]
    return {"pairs": pairs, "vif": dict(zip(names, factors)), "warning": bool(strong or high),
            "strong_pairs": strong, "high_vif": high, "corr_limit": CORR_LIMIT, "vif_limit": VIF_LIMIT}
