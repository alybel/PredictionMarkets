"""Stock evaluation of metric indices (1015-2231): timing without look-ahead, simple and multiple regression, F-test,
multicollinearity warning, excess return, multiple testing, endpoints, point-in-time guard. No network, no LLM."""

import logging
import math
import random
from datetime import date, datetime, time, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from backend.app.api import app, get_markets, get_session
from backend.app.builder.models import MetricIndex
from backend.app.eui.models import MarketSnapshot, SnapshotRun
from backend.app.fed.api import get_fed_source
from backend.app.metrics.models import VALIDATED, MetricState
from backend.app.metrics.service import create_basket
from backend.app.models import utcnow
from backend.app.stocks.correlation import MAX_GAP_DAYS, next_day_returns, signal_days
from backend.app.stocks.metric_data import MetricInputs, excess_returns, load_inputs, market_returns, observations
from backend.app.stocks.metric_regression import MARKET_TICKER, finite
from backend.app.stocks.models import StockPrice
from backend.app.stocks.ols import collinearity, ols
from backend.app.stocks.stats import correlation_p_value, f_p_value, t_p_value
from backend.tests.eui_helpers import memory_session
from backend.tests.test_metric_catalog import EmptySource
from backend.tests.test_stock_correlation import _weekdays

D = [date(2026, 3, 2) + timedelta(n) for n in range(30)]  # 2 March 2026 is a Monday


def noise(i: int, scale: float = 1.0) -> float:
    return scale * math.sin(i * 12.9898) * 0.5  # deterministic, roughly uncorrelated with smooth regressors


# ---- timing ---------------------------------------------------------------------------------------------------


def test_observations_pair_day_t_with_the_return_close_t_to_next_close():
    days = [D[0], D[1], D[2], D[4]]  # Mon, Tue, Wed, Fri (Thu missing in the index, not in the prices)
    inputs = MetricInputs("x", {d: v for d, v in zip(days, [0.0, 1.0, 0.5, 2.0])},
                          {d: [v, -v] for d, v in zip(days, [10.0, 12.0, 11.0, 15.0])}, [], [], 0)
    closes = {D[i]: c for i, c in zip((0, 1, 2, 3, 4, 7), (100, 110, 99, 99, 100, 105))}
    obs = observations(inputs, set(closes), next_day_returns(closes))
    assert [(o["day"], o["exit"]) for o in obs] == [(D[1], D[2]), (D[2], D[3]), (D[4], D[7])]
    first = obs[0]
    assert first["dz"] == 1.0 and first["dx"] == [2.0, -2.0] and first["ret"] == pytest.approx(99 / 110 - 1)
    assert obs[2]["dz"] == 1.5 and obs[2]["dx"] == [4.0, -4.0]  # Wed -> Fri: change carried over the missing Thursday


def test_no_pairs_across_data_gaps():
    far = D[0] + timedelta(MAX_GAP_DAYS + 1)
    inputs = MetricInputs("x", {D[0]: 0.0, far: 1.0, far + timedelta(1): 2.0}, {D[0]: [0.0], far: [1.0], far + timedelta(1): [2.0]}, [], [], 0)
    closes = {D[0]: 100.0, far: 100.0, far + timedelta(1): 101.0, far + timedelta(1 + MAX_GAP_DAYS + 1): 102.0}
    assert observations(inputs, set(closes), next_day_returns(closes)) == []  # index gap and price gap are both skipped


def _old_signal_days(index_values, closes):
    """Reference: the archive implementation before 1015-2231."""
    trading_days = sorted(closes)
    next_day = dict(zip(trading_days, trading_days[1:]))
    days = [d for d in sorted(index_values) if d in closes]
    rows = []
    for prev, day in zip(days, days[1:]):
        nxt = next_day.get(day)
        if nxt is None or (day - prev).days > MAX_GAP_DAYS or (nxt - day).days > MAX_GAP_DAYS or closes[day] <= 0:
            continue
        rows.append((day, nxt, index_values[day] - index_values[prev], closes[nxt] / closes[day] - 1))
    return rows


def test_archive_signal_days_are_unchanged_by_the_refactoring():
    rnd = random.Random(7)
    for _ in range(50):
        span = [date(2026, 1, 1) + timedelta(n) for n in range(80)]
        closes = {d: rnd.uniform(50, 150) for d in span if rnd.random() < 0.7}
        values = {d: rnd.random() for d in span if rnd.random() < 0.6}
        assert signal_days(values, closes) == _old_signal_days(values, closes)


# ---- statistics ------------------------------------------------------------------------------------------------


def test_t_and_f_tail_probabilities_match_reference_values():
    assert t_p_value(2.0, 10) == pytest.approx(0.07339, abs=1e-4)
    assert t_p_value(2.086, 20) == pytest.approx(0.05, abs=1e-3)
    assert f_p_value(4.0, 1, 10) == pytest.approx(0.07339, abs=1e-4)  # F(1, df) = t²
    assert f_p_value(3.4928, 2, 20) == pytest.approx(0.05, abs=1e-3)


def test_simple_regression_recovers_the_known_slope_and_p_value():
    xs = [float(i % 7 - 3) for i in range(40)]
    ys = [0.004 * x + 0.002 * noise(i) for i, x in enumerate(xs)]
    fit = ols([[x] for x in xs], ys)
    assert fit["coefficients"][0] == pytest.approx(0.004, abs=3e-4)
    r = math.copysign(math.sqrt(fit["r2"]), fit["coefficients"][0])
    assert fit["p_values"][0] == pytest.approx(correlation_p_value(r, 40), rel=1e-6)  # t-test of b = test of r
    assert fit["f_p_value"] == pytest.approx(fit["p_values"][0], rel=1e-6) and fit["p_values"][0] < 1e-6


def test_multiple_regression_recovers_two_coefficients_f_test_and_r2():
    x1 = [math.sin(i * 0.5) for i in range(60)]
    x2 = [math.cos(i * 0.3) for i in range(60)]
    ys = [0.01 + 2 * a - 3 * b + 0.05 * noise(i) for i, (a, b) in enumerate(zip(x1, x2))]
    fit = ols([[a, b] for a, b in zip(x1, x2)], ys)
    assert fit["coefficients"] == pytest.approx([2, -3], abs=0.05) and fit["intercept"] == pytest.approx(0.01, abs=0.02)
    assert fit["r2"] > 0.99 and fit["f_p_value"] < 1e-10 and max(fit["p_values"]) < 1e-10 and fit["df"] == 57
    pure_noise = ols([[a, b] for a, b in zip(x1, x2)], [noise(i) for i in range(60)])
    assert pure_noise["r2"] < 0.2 and pure_noise["f_p_value"] > 0.05
    assert ols([[1.0, 2.0]] * 3, [1.0, 2.0, 3.0]) is None  # fewer observations than parameters + 1


def test_multicollinearity_warns_only_for_nearly_identical_regressors():
    x1 = [math.sin(i * 0.5) for i in range(60)]
    twin = collinearity([[a, a + 0.001 * noise(i)] for i, a in enumerate(x1)], ["a", "b"])
    assert twin["warning"] and twin["strong_pairs"][0]["correlation"] > 0.99 and twin["vif"]["a"] > 100
    independent = collinearity([[a, math.cos(i * 0.3)] for i, a in enumerate(x1)], ["a", "b"])
    assert not independent["warning"] and independent["vif"]["a"] < 2
    exact = collinearity([[a, 2 * a] for a in x1], ["a", "b"])
    assert exact["warning"] and finite(exact)["vif"]["a"] is None  # ∞ is sent as null


# ---- excess return ---------------------------------------------------------------------------------------------


def test_excess_return_is_stock_minus_equal_weighted_market():
    rets = {"A": {D[0]: (D[1], 0.03), D[1]: (D[2], -0.01)}, "B": {D[0]: (D[1], 0.01), D[1]: (D[2], 0.01)},
            "C": {D[0]: (D[1], -0.01)}}
    market = market_returns(rets)
    assert market[D[0]] == (D[1], pytest.approx(0.01)) and market[D[1]] == (D[2], pytest.approx(0.0))
    assert excess_returns(rets["A"], market) == {D[0]: (D[1], pytest.approx(0.02)), D[1]: (D[2], pytest.approx(-0.01))}
    assert excess_returns({D[0]: (D[2], 0.05)}, market) == {}  # different window (data hole) -> no excess return


# ---- endpoints --------------------------------------------------------------------------------------------------


def _store_basket(session, ext, prices, days):
    for day, price in zip(days, prices):
        run = SnapshotRun(snapshot_date=day, status="ok")
        session.add(run)
        session.flush()
        session.add(MarketSnapshot(run_id=run.id, snapshot_date=day, source="polymarket", external_id=ext, title=ext,
                                   tags=["Economics"], price=price, volume_24h_usd=1000.0,
                                   fetched_at=datetime.combine(day, time(8), tzinfo=timezone.utc)))


def _closes(start, rets_by_day, days):
    out, close = {}, start
    for day in days:
        out[day] = close
        close *= 1 + rets_by_day.get(day, 0.0)
    return out


@pytest.fixture()
def env():
    session = memory_session()
    today = utcnow().date()
    days = [d for d in _weekdays(today - timedelta(110), 90) if d <= today - timedelta(3)]
    _store_basket(session, "pm-a", [0.5 + 0.1 * math.sin(i * 1.3) for i in range(len(days))], days)
    _store_basket(session, "pm-b", [0.4 + 0.1 * math.cos(i * 0.7) for i in range(len(days))], days)
    for key, ext in (("b_one", "pm-a"), ("b_two", "pm-b")):
        create_basket(session, key, key, "", [{"source": "polymarket", "external_id": ext, "title": ext}], None)
        session.get(MetricState, key).status = VALIDATED
    row = MetricIndex(name="Fed Rates", components=[{"key": "b_one", "weight": 1.0}, {"key": "b_two", "weight": 1.0}])
    session.add(row)
    session.commit()
    inputs = load_inputs(session, row, 365, today)
    zdays = sorted(inputs.values)
    dz = {d: inputs.values[d] - inputs.values[p] for p, d in zip(zdays, zdays[1:])}
    planted = {"AAPL": {d: 0.01 * v for d, v in dz.items()}, "MSFT": {d: -0.01 * v for d, v in dz.items()},
               "NVDA": {d: 0.004 * noise(i) for i, d in enumerate(days)}}
    for ticker, rets in planted.items():
        session.add_all(StockPrice(ticker=ticker, price_date=d, close=c) for d, c in _closes(100.0, rets, days).items())
    session.commit()
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_markets] = lambda: []
    app.dependency_overrides[get_fed_source] = EmptySource
    yield TestClient(app), session, inputs
    app.dependency_overrides.clear()


def test_metric_index_endpoint_returns_the_full_structure(env, caplog):
    client, _, inputs = env
    with caplog.at_level(logging.INFO):
        body = client.get("/builder/indices/Fed Rates/correlation?days=365").json()
    assert body["kind"] == "metric" and body["unit"] == "z" and body["target"] == "excess"  # excess is the default
    assert len(body["stocks"]) == 100 and body["market"]["ticker"] == MARKET_TICKER and body["market"]["target"] == "raw"
    assert [c["key"] for c in body["components"]] == ["b_one", "b_two"] and [c["label"] for c in body["components"]] == ["Pkt.", "Pkt."]
    assert body["index_days"] == len(inputs.values) and body["stocks_with_prices"] == 3 and body["stocks_tested"] == 3
    aapl = next(r for r in body["stocks"] if r["ticker"] == "AAPL")
    for key in ("correlation", "p_value", "slope_per_point", "r2", "multi_r2", "multi_f_p", "precision", "signals", "bonferroni"):
        assert key in aapl
    assert len(aapl["coefficients"]) == 2 and {"coef", "p_value", "std_error", "bonferroni"} <= set(aapl["coefficients"][0])
    assert {"warning", "vif", "pairs"} <= set(body["collinearity"])
    raw = client.get("/builder/indices/Fed Rates/correlation?days=365&target=raw").json()
    aapl_raw = next(r for r in raw["stocks"] if r["ticker"] == "AAPL")
    assert aapl_raw["correlation"] == pytest.approx(1.0) and aapl_raw["slope_per_point"] == pytest.approx(0.01)  # per z-point
    assert aapl_raw["r2"] == pytest.approx(1.0) and aapl_raw["multi_r2"] > 0.95  # z is not exactly linear in the raw values
    assert raw["market"]["correlation"] == pytest.approx(body["market"]["correlation"])  # market row always raw
    assert "metric correlation of 'Fed Rates': days=365 target=excess" in caplog.text


def test_multiple_testing_thresholds_match_the_number_of_tested_stocks(env):
    client, _, _ = env
    body = client.get("/builder/indices/Fed Rates/correlation?days=365&target=raw").json()
    assert body["bonferroni_alpha"] == pytest.approx(0.05 / body["stocks_tested"])
    assert body["expected_false_positives"] == pytest.approx(round(0.05 * body["stocks_tested"], 1))
    assert body["regression_tested"] == 3 and body["regression_bonferroni_alpha"] == pytest.approx(0.05 / 3)
    assert body["coefficient_tests"] == 6 and body["coefficient_bonferroni_alpha"] == pytest.approx(0.05 / 6)
    assert body["regression_expected_false_positives"] == pytest.approx(0.2)


def test_min_change_is_in_z_points(env):
    client, _, inputs = env
    body = client.get("/builder/indices/Fed Rates/correlation?days=365&target=raw&min_change=0.5").json()
    aapl = next(r for r in body["stocks"] if r["ticker"] == "AAPL")
    zdays = sorted(inputs.values)
    big = [inputs.values[d] - inputs.values[p] for p, d in zip(zdays, zdays[1:]) if abs(inputs.values[d] - inputs.values[p]) > 0.5]
    assert body["min_change"] == 0.5 and aapl["events_up"] + aapl["events_down"] <= len(big)
    assert client.get("/builder/indices/Fed Rates/correlation?min_change=6").status_code == 422


def test_stock_detail_opens_for_metric_indices(env, caplog):
    client, _, _ = env
    with caplog.at_level(logging.INFO):
        body = client.get("/builder/indices/Fed Rates/correlation/AAPL?days=365&target=raw").json()
    assert body["ticker"] == "AAPL" and body["unit"] == "z" and body["series"] and body["summary"]["trades"] > 0
    assert [c["min_change"] for c in body["robustness"][::3]] == [0.25, 0.5, 1.0]  # grid in z-points
    reg = body["regression"]
    assert reg["simple"]["coef"] == pytest.approx(0.01) and reg["simple"]["r2"] == pytest.approx(1.0)
    assert len(reg["multiple"]["coefficients"]) == 2 and "warning" in reg["collinearity"]
    assert "metric trades of 'Fed Rates' vs AAPL" in caplog.text


def test_unknown_index_ticker_or_target(env):
    client, _, _ = env
    assert client.get("/builder/indices/nope/correlation").status_code == 404
    assert client.get("/builder/indices/nope/correlation/AAPL").status_code == 404
    assert client.get("/builder/indices/Fed Rates/correlation/XXXX").status_code == 404
    assert client.get("/builder/indices/Fed Rates/correlation?target=alpha").status_code == 422


def test_provisional_or_future_dated_data_never_enters(env):
    client, session, inputs = env
    before = client.get("/builder/indices/Fed Rates/correlation?days=365&target=raw").json()
    today = utcnow().date()
    last = max(inputs.values)
    # a provisional (intraday) close of the last day and a future-dated close must be ignored
    session.add(StockPrice(ticker="AAPL", price_date=today + timedelta(5), close=1.0))
    session.query(StockPrice).filter_by(ticker="MSFT", price_date=last).update(
        {"close": 1.0, "fetched_at": datetime.combine(last, time(12), tzinfo=timezone.utc)})
    _store_basket(session, "pm-a", [0.99], [today + timedelta(4)])
    _store_basket(session, "pm-b", [0.01], [today + timedelta(4)])
    session.commit()
    after = client.get("/builder/indices/Fed Rates/correlation?days=365&target=raw").json()
    assert after["index_days"] == before["index_days"]
    for ticker in ("AAPL",):
        a = next(r for r in after["stocks"] if r["ticker"] == ticker)
        b = next(r for r in before["stocks"] if r["ticker"] == ticker)
        assert a["n"] == b["n"] and a["correlation"] == pytest.approx(b["correlation"])
    msft = next(r for r in after["stocks"] if r["ticker"] == "MSFT")
    assert msft["n"] == next(r for r in before["stocks"] if r["ticker"] == "MSFT")["n"] - 1  # provisional close dropped


# ---- frontend wiring -------------------------------------------------------------------------------------------


def test_frontend_wires_metric_indices_into_the_stock_card():
    from pathlib import Path
    static = Path(__file__).resolve().parents[1] / "app" / "static"
    html, js = (static / "index.html").read_text(), (static / "regression.js").read_text()
    assert "/static/regression.js" in html and 'id="cr-target"' in html and 'id="mi-cr-slot"' in html
    assert 'value="excess"' in html.split('id="cr-target"')[1].split("</select>")[0].split("<option")[1]  # default first
    assert 'id="cr-mc-warning"' in html and 'id="cr-reg-simple"' in html and 'id="cr-reg-multiple"' in html
    assert '"correlation.target"' in js and "market-row" in js and "Multikollinearität" in js
    assert "/builder/indices/" in (static / "correlation.js").read_text()
    assert "showMetricCorrelation(name)" in (static / "metricindex.js").read_text()
    assert "kind: corr.kind" in (static / "trades.js").read_text()  # stock click logged with the index kind
