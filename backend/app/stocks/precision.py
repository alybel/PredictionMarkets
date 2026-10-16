"""Signal precision with a stock-move threshold, measured out of sample (feature 1015-1157).

Signal = trading day T with |ΔI_T| > min_change; hit = next-day return beyond min_stock_move in the expected
direction (same timing as the correlation, no look-ahead). The expected direction (with or against the index)
comes only from the first chronological half of the paired days (sign of the correlation there); every
figure below is measured only on the second half, so the direction choice gives no in-sample advantage.
Both index directions are pooled: an index rise expects ``direction``, a fall expects ``-direction``.

Base rate = share of all test days on which the stock moved beyond the threshold in the expected direction
anyway, weighted by the mix of up/down signals; p_signal is the one-sided binomial test of the hits against it.
"""

from backend.app.stocks.stats import EPSILON, binomial_sf, pearson

MIN_SIGNALS = 15                           # fewer test signals: "too little data"
ROBUST_INDEX_CHANGES = (0.005, 0.01, 0.02)  # 0.5 / 1 / 2 index points (0..1 scale)
ROBUST_STOCK_MOVES = (0.005, 0.01, 0.02)    # 0.5 / 1 / 2 %


def expected_direction(train: list[tuple[float, float]]) -> int | None:
    """+1 (stock follows the index) or −1 (moves against it) from the training half; None if undetermined."""
    r, _ = pearson([c for c, _ in train], [ret for _, ret in train])
    return None if not r else (1 if r > 0 else -1)


def signal_precision(pairs: list[tuple[float, float]], min_change: float, min_stock_move: float) -> dict:
    """Out-of-sample precision of chronological (ΔI_T, next-day return) pairs; split 50/50 into train/test."""
    half = len(pairs) // 2
    train, test = pairs[:half], pairs[half:]
    direction = expected_direction(train)
    out = {"expected_direction": direction, "signals": 0, "signal_hits": 0, "precision": None,
           "base_rate": None, "lift": None, "p_signal": None}
    if direction is None or not test:
        return out
    threshold = min_stock_move + EPSILON
    share_with = sum(1 for _, ret in test if direction * ret > threshold) / len(test)
    share_against = sum(1 for _, ret in test if -direction * ret > threshold) / len(test)
    signals = [(c, ret) for c, ret in test if abs(c) > min_change + EPSILON]
    ups = sum(1 for c, _ in signals if c > 0)
    hits = sum(1 for c, ret in signals if direction * (1 if c > 0 else -1) * ret > threshold)
    base = ((ups * share_with + (len(signals) - ups) * share_against) / len(signals) if signals
            else (share_with + share_against) / 2)
    out.update(signals=len(signals), signal_hits=hits, base_rate=base)
    if len(signals) >= MIN_SIGNALS:
        precision = hits / len(signals)
        out.update(precision=precision, lift=precision / base if base > 0 else None,
                   p_signal=binomial_sf(hits, len(signals), base))
    return out


def robustness_matrix(pairs: list[tuple[float, float]], index_changes=ROBUST_INDEX_CHANGES) -> list[dict]:
    """3×3 grid index threshold × stock threshold — to judge stability, never to pick the best cell afterwards."""
    return [{"min_change": c, "min_stock_move": m, **signal_precision(pairs, c, m)}
            for c in index_changes for m in ROBUST_STOCK_MOVES]
