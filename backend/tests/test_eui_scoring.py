"""Uncertainty contribution, square-root weights with cap, 0-100 scale."""

import pytest

from backend.app.eui.models import POLARITY_BAD_IF_NO, POLARITY_BAD_IF_YES, POLARITY_SYMMETRIC
from backend.app.eui.scoring import cap_weights, contribution, index_value, renormalize, sqrt_weights, weights_from_scores


def test_contribution_by_polarity():
    assert contribution(0.5, POLARITY_SYMMETRIC) == 1.0
    assert contribution(0.9, POLARITY_SYMMETRIC) == pytest.approx(0.2)
    assert contribution(0.1, POLARITY_SYMMETRIC) == pytest.approx(0.2)
    assert contribution(0.9, POLARITY_BAD_IF_YES) == 0.9
    assert contribution(0.9, POLARITY_BAD_IF_NO) == pytest.approx(0.1)
    with pytest.raises(ValueError):
        contribution(0.5, "sideways")


def test_sqrt_weights_and_cap_redistribution():
    assert sqrt_weights([4.0, 1.0]) == pytest.approx([2 / 3, 1 / 3])
    assert sqrt_weights([0.0, 0.0]) == [0.5, 0.5]
    weights = weights_from_scores([10_000.0] + [1.0] * 19)
    assert max(weights) == pytest.approx(0.10)
    assert sum(weights) == pytest.approx(1.0)
    assert weights[1] == pytest.approx((1 - 0.10) / 19)
    # With fewer than ten constituents the cap cannot be honoured and weights stay as they are.
    assert cap_weights([0.5, 0.5]) == [0.5, 0.5]


def test_renormalize_after_dropouts_keeps_cap():
    kept = [0.10, 0.10, 0.02] + [0.06] * 10  # sums to 0.82 after some constituents dropped out
    weights = renormalize(kept)
    assert sum(weights) == pytest.approx(1.0)
    assert max(weights) == pytest.approx(0.10)


def test_index_value_scale():
    assert index_value([], []) is None
    assert index_value([0.5, 0.5], [1.0, 1.0]) == 100.0
    assert index_value([0.5, 0.5], [0.0, 0.0]) == 0.0
    assert index_value([0.25, 0.75], [1.0, 0.0]) == pytest.approx(25.0)
