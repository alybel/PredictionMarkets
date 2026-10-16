"""Signal precision with a stock-move threshold (1015-1157): out-of-sample split, base rate, lift, binomial test. No network."""

import logging
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.app.api import app, get_markets, get_session
from backend.app.engine import get_rule, upsert_rule
from backend.app.stocks.correlation import closes_by_ticker, index_values, signal_pairs, stock_stats
from backend.app.stocks.models import StockPrice
from backend.app.stocks.precision import MIN_SIGNALS, robustness_matrix, signal_precision
from backend.app.stocks.stats import binomial_sf
from backend.tests.eui_helpers import memory_session
from backend.tests.test_index_builder import snap
from backend.tests.test_stock_correlation import _weekdays

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"
TRAIN_WITH = [(0.02, 0.01), (-0.02, -0.01)] * 10      # first half: stock follows the index
TRAIN_AGAINST = [(0.02, -0.01), (-0.02, 0.01)] * 10   # first half: stock moves against the index
# second half, 20 days: 10 rises (6 hits), 5 falls (3 hits), 5 days below min_change
TEST = [(0.02, 0.02)] * 6 + [(0.02, -0.02)] * 4 + [(-0.02, -0.02)] * 3 + [(-0.02, 0.005)] * 2 + [(0.001, 0.02)] * 5
REVERSED = [(c, -r) for c, r in TEST]                 # same days, every return mirrored


def test_binomial_sf_reference_values():
    assert binomial_sf(25, 40, 0.5) == pytest.approx(0.0769, abs=1e-4)  # scipy.stats.binom.sf(24, 40, 0.5)
    assert binomial_sf(0, 10, 0.3) == 1.0 and binomial_sf(11, 10, 0.3) == 0.0
    assert binomial_sf(10, 10, 0.5) == pytest.approx(0.5 ** 10)


def test_precision_base_rate_and_lift_match_hand_calculation():
    stats = signal_precision(TRAIN_WITH + TEST, min_change=0.005, min_stock_move=0.01)
    # base: 11 of 20 test days > +1 %, 7 of 20 < −1 %; weighted with 10 rises / 5 falls -> (10·0.55 + 5·0.35) / 15
    assert stats["expected_direction"] == 1 and stats["signals"] == 15 and stats["signal_hits"] == 9
    assert stats["precision"] == pytest.approx(0.6) and stats["base_rate"] == pytest.approx(7.25 / 15)
    assert stats["lift"] == pytest.approx(0.6 / (7.25 / 15))
    assert stats["p_signal"] == pytest.approx(binomial_sf(9, 15, 7.25 / 15))


def test_direction_comes_only_from_the_first_half():
    stats = signal_precision(TRAIN_WITH + REVERSED, 0.005, 0.01)  # pattern flips in the second half
    # only the 4 rises followed by +2 % remain hits -> no in-sample advantage from the flipped pattern
    assert stats["expected_direction"] == 1 and stats["signal_hits"] == 4 and stats["precision"] == pytest.approx(4 / 15)
    assert stats["precision"] < signal_precision(TRAIN_WITH + TEST, 0.005, 0.01)["precision"]


def test_opposite_stock_is_scored_against_the_index():
    stats = signal_precision(TRAIN_AGAINST + REVERSED, 0.005, 0.01)
    assert stats["expected_direction"] == -1 and stats["signal_hits"] == 9 and stats["precision"] == pytest.approx(0.6)


def test_too_few_signals_give_no_precision():
    stats = signal_precision(TRAIN_WITH + TEST[:14] + [(0.001, 0.0)] * 6, 0.005, 0.01)
    assert stats["signals"] == MIN_SIGNALS - 1 and stats["base_rate"] is not None
    assert stats["precision"] is None and stats["lift"] is None and stats["p_signal"] is None


def test_move_exactly_on_the_threshold_is_no_hit():
    on_edge = 1.01 / 1.0 - 1  # 0.010000000000000009 in floating point
    stats = signal_precision(TRAIN_WITH + [(0.02, on_edge)] * 20, 0.005, 0.01)
    assert stats["signals"] == 20 and stats["signal_hits"] == 0 and stats["precision"] == 0


def test_robustness_matrix_has_nine_cells_with_signal_counts():
    test = [(0.007, 0.03)] * 6 + [(0.015, 0.03)] * 5 + [(-0.03, -0.03)] * 4 + [(0.003, 0.0)] * 5
    cells = robustness_matrix(TRAIN_WITH + test)
    assert [(c["min_change"], c["min_stock_move"]) for c in cells] == [
        (i, s) for i in (0.005, 0.01, 0.02) for s in (0.005, 0.01, 0.02)]
    assert [c["signals"] for c in cells] == [15] * 3 + [9] * 3 + [4] * 3
    assert cells[0]["precision"] == 1.0 and cells[3]["precision"] is None


# ---- API ------------------------------------------------------------------------------

@pytest.fixture()
def client():
    session = memory_session()
    upsert_rule(session, "econ", categories=["economics"])
    days = _weekdays(date(2026, 3, 2), 64)
    values = [0.5 + 0.02 * ((i * 7) % 5 - 2) for i in range(len(days))]
    for day, value in zip(days, values):
        snap(session, day, "m", price=value)
    # AAPL's next-day return is 0.5 x the index change; MSFT alternates ±0.8 % regardless of the index
    aapl, msft = [100.0], [100.0]
    for i in range(1, len(days)):
        change = values[i - 1] - values[i - 2] if i >= 2 else 0.0
        aapl.append(aapl[-1] * (1 + 0.5 * change))
        msft.append(msft[-1] * (1 + (0.008 if i % 2 else -0.008)))
    for day, a, m in zip(days, aapl, msft):
        session.add_all([StockPrice(ticker="AAPL", price_date=day, close=a), StockPrice(ticker="MSFT", price_date=day, close=m)])
    session.commit()
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_markets] = lambda: []
    yield TestClient(app), session
    app.dependency_overrides.clear()


def test_correlation_api_adds_precision_and_bonferroni_flag(client, caplog):
    http, _ = client
    with caplog.at_level(logging.INFO):
        body = http.get("/indices/econ/correlation?min_stock_move=0.005").json()
    rows = {row["ticker"]: row for row in body["stocks"]}
    assert body["min_stock_move"] == 0.005 and body["min_signals"] == MIN_SIGNALS and body["signal_tested"] == 2
    assert body["signal_bonferroni_alpha"] == pytest.approx(0.025)
    assert rows["AAPL"]["precision"] == 1.0 and rows["AAPL"]["signal_significant"]
    assert not rows["MSFT"]["signal_significant"] and rows["MSFT"]["p_signal"] > 0.025
    for row in body["stocks"]:
        assert row["signal_significant"] == (row["p_signal"] is not None and row["p_signal"] < 0.025)
    assert body["total_signals"] == rows["AAPL"]["signals"] + rows["MSFT"]["signals"]
    assert "test signals in total" in caplog.text and "below 15 signals" in caplog.text


def test_existing_correlation_fields_are_unchanged(client):
    http, session = client
    body = http.get("/indices/econ/correlation").json()
    assert body["min_stock_move"] == 0.01  # default 1 %
    old_keys = {"n", "correlation", "p_value", "significant", "slope_per_point", "events_up", "events_down",
                "hit_rate", "mean_return_after_up", "mean_return_after_down"}
    values = index_values(session, get_rule(session, "econ"), 90)
    closes = closes_by_ticker(session, min(values))
    row = next(r for r in body["stocks"] if r["ticker"] == "AAPL")
    expected = stock_stats(signal_pairs(values, closes["AAPL"]))
    assert {k: row[k] for k in old_keys} == pytest.approx(expected)


def test_min_stock_move_is_validated_on_both_endpoints(client):
    http, _ = client
    assert http.post("/indices/econ/correlation/preview?min_stock_move=0.02", json={"categories": ["economics"]}).json()["min_stock_move"] == 0.02
    for bad in ("-0.01", "2", "abc"):
        assert http.get(f"/indices/econ/correlation?min_stock_move={bad}").status_code == 422
        assert http.post(f"/indices/econ/correlation/preview?min_stock_move={bad}", json={}).status_code == 422


def test_short_history_gives_null_precision_and_trades_carry_the_matrix(client):
    http, _ = client
    body = http.get("/indices/econ/correlation?days=20").json()
    row = next(r for r in body["stocks"] if r["ticker"] == "AAPL")
    assert row["signals"] < MIN_SIGNALS and row["precision"] is None and row["lift"] is None and row["p_signal"] is None
    assert body["stocks_below_min_signals"] == 2
    detail = http.get("/indices/econ/correlation/AAPL").json()
    assert len(detail["robustness"]) == 9 and detail["min_signals"] == MIN_SIGNALS


def test_frontend_has_precision_columns_and_matrix():
    html = (STATIC / "index.html").read_text()
    js = (STATIC / "correlation.js").read_text()
    trades = (STATIC / "trades.js").read_text()
    assert 'id="cr-min-stock-move"' in html and 'id="cr-robust-rows"' in html
    assert all(f'data-cr-sort="{k}"' in html for k in ("signals", "precision", "base_rate", "lift", "p_signal"))
    assert "minStockMove: 0.01" in js and "min_stock_move=" in js and "correlation.min_stock_move" in js
    assert "zu wenig Daten" in js and "gegenläufig" in js and '" von "' in js and "signal_significant" in js
    assert "robustness" in trades and "nicht nachträglich ausgewählt" in trades
