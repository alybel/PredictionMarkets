"""Component attribution (1015-1200): exact split of ΔI and of correlation/beta per index market. No network."""

import random
from datetime import date, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.app.api import app, get_markets, get_session
from backend.app.engine import upsert_rule
from backend.app.history import SnapshotMarket
from backend.app.stocks.attribution import SHARE_KEYS, attribute, component_weights, day_effects
from backend.app.stocks.models import StockPrice
from backend.tests.eui_helpers import memory_session
from backend.tests.test_index_builder import snap
from backend.tests.test_stock_correlation import _weekdays

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"
TOL = 1e-9
DAYS = _weekdays(date(2026, 3, 2), 30)


def _market(ext, price, liquidity):
    return SnapshotMarket(source="polymarket", external_id=ext, title=ext, price=price, liquidity=liquidity, end_date=None)


def _value(weights):
    return sum(w * p for w, p in weights.values())


def _random_index(seed=1, churn=True):
    """30 weekdays of a 6-market index with random prices/turnover; with ``churn`` markets enter and exit."""
    rng = random.Random(seed)
    weights = {}
    for i, day in enumerate(DAYS):
        markets = [_market(f"m{k}", rng.uniform(0.05, 0.95), rng.uniform(1e4, 1e5) if churn else 1.0)
                   for k in range(6) if not churn or (k + i) % 7 or k == 0]
        weights[day] = component_weights(markets)
    values = {d: _value(w) for d, w in weights.items()}
    closes, close = {}, 100.0
    for day in DAYS:
        closes[day] = close
        close *= 1 + rng.gauss(0, 0.02)
    return values, weights, closes


def _check_sums(result):
    total = result["total"]
    assert total["corr_total"] == pytest.approx(result["correlation"], abs=TOL)
    assert total["beta_total"] == pytest.approx(result["slope_per_point"], abs=TOL)
    for share in ("corr", "beta"):
        assert total[f"{share}_price"] + total[f"{share}_weight"] == pytest.approx(total[f"{share}_total"], abs=TOL)


# ---- decomposition --------------------------------------------------------------------

def test_weights_match_the_aggregation_incl_simple_average_fallback():
    weights = component_weights([_market("a", 0.2, 3.0), _market("b", 0.6, 1.0), _market("c", None, 9.0)])
    assert weights == {"polymarket:a": (0.75, 0.2), "polymarket:b": (0.25, 0.6)} and _value(weights) == pytest.approx(0.3)
    assert component_weights([_market("a", 0.2, 0.0), _market("b", 0.6, None)])["polymarket:a"] == (0.5, 0.2)


def test_effects_sum_exactly_to_index_change_with_entries_and_exits():
    values, weights, _ = _random_index()
    for prev, day in zip(DAYS, DAYS[1:]):
        effects = day_effects(weights[prev], weights[day])
        assert sum(p + w for p, w in effects.values()) == pytest.approx(values[day] - values[prev], abs=1e-12)


def test_constant_weights_give_zero_weight_effect():
    prev = {"a": (0.5, 0.2), "b": (0.5, 0.6)}
    effects = day_effects(prev, {"a": (0.5, 0.3), "b": (0.5, 0.4)})
    assert all(w == 0 for _, w in effects.values()) and effects["a"][0] == pytest.approx(0.05)


def test_entry_and_exit_land_fully_in_weight_effect():
    effects = day_effects({"a": (1.0, 0.4)}, {"a": (0.5, 0.5), "new": (0.5, 0.8)})
    assert effects["new"] == (0.0, pytest.approx(0.4))  # entry day: weight × price, no price effect
    assert effects["a"] == (pytest.approx(0.1), pytest.approx(-0.25))
    assert day_effects({"gone": (1.0, 0.3)}, {"b": (1.0, 0.6)})["gone"] == (0.0, pytest.approx(-0.3))


# ---- covariance attribution -----------------------------------------------------------

def test_shares_sum_to_correlation_and_beta():
    for seed in (1, 2, 3):
        result = attribute(*_random_index(seed))
        assert result["correlation"] is not None and result["n"] == len(DAYS) - 2  # the last day has no next close
        _check_sums(result)


def test_constant_weights_give_zero_weight_share():
    result = attribute(*_random_index(churn=False))
    assert all(abs(r["corr_weight"]) < TOL and abs(r["beta_weight"]) < TOL for r in result["components"])
    _check_sums(result)


def test_single_component_carries_100_percent():
    values, _, closes = _random_index()
    weights = {d: {"only": (1.0, v)} for d, v in values.items()}
    result = attribute(values, weights, closes)
    [row] = result["components"]
    assert row["corr_total"] == pytest.approx(result["correlation"], abs=TOL) and row["corr_weight"] == 0
    assert row["days_in_index"] == len(DAYS) - 1 and row["avg_weight"] == 1.0
    assert row["own_correlation"] == pytest.approx(result["correlation"]) and result["others"] is None


def test_component_on_a_single_day_is_counted():
    values, weights, closes = _random_index(churn=False)
    one_day = DAYS[10]
    weights[one_day] = {**{k: (w * 0.9, p) for k, (w, p) in weights[one_day].items()}, "flash": (0.1, 0.9)}
    values[one_day] = _value(weights[one_day])
    result = attribute(values, weights, closes)
    flash = next(r for r in result["components"] if r["key"] == "flash")
    assert flash["days_in_index"] == 1 and flash["avg_weight"] == pytest.approx(0.1)
    assert flash["corr_price"] == 0 and flash["own_n"] == 2 and flash["own_correlation"] is None  # entry + exit day
    _check_sums(result)


def test_top_cap_puts_the_rest_into_others():
    result = attribute(*_random_index(), top=2)
    assert len(result["components"]) == 2 and result["others"]["count"] == result["component_count"] - 2
    shown = {k: sum(r[k] for r in result["components"]) for k in SHARE_KEYS}
    for k in SHARE_KEYS:
        assert shown[k] + result["others"][k] == pytest.approx(result["total"][k], abs=TOL)
    assert abs(result["components"][0]["corr_total"]) >= abs(result["components"][1]["corr_total"])


def test_too_few_observations_and_missing_prices_give_no_shares():
    values, weights, closes = _random_index()
    few = attribute({d: values[d] for d in DAYS[:10]}, weights, closes)  # 9 pairs < 10
    assert few["n"] == 9 and few["correlation"] is None and few["components"] == [] and few["total"] is None
    empty = attribute(values, weights, {})
    assert empty["n"] == 0 and empty["components"] == [] and empty["others"] is None


# ---- API ------------------------------------------------------------------------------

@pytest.fixture()
def env():
    session = memory_session()
    upsert_rule(session, "econ", categories=["economics"])
    rng = random.Random(7)
    for i, day in enumerate(DAYS):
        for k in range(4):
            if k < 2 or i % (k + 2):  # m2/m3 leave and re-enter the index
                snap(session, day, f"m{k}", price=rng.uniform(0.1, 0.9), volume_24h=rng.uniform(2e4, 9e4))
    close = 100.0
    for day in DAYS:
        session.add(StockPrice(ticker="AAPL", price_date=day, close=close))
        close *= 1 + rng.gauss(0, 0.02)
    session.commit()
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_markets] = lambda: []
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_attribution_endpoint_matches_the_main_table(env, caplog):
    caplog.set_level("INFO", logger="backend.app.stocks.attribution")
    body = env.get("/indices/econ/correlation/AAPL/attribution?min_change=0.01").json()
    main = next(r for r in env.get("/indices/econ/correlation").json()["stocks"] if r["ticker"] == "AAPL")
    assert body["n"] == main["n"] == len(DAYS) - 2 and body["min_change"] == 0.01
    assert body["total"]["corr_total"] == pytest.approx(main["correlation"], abs=TOL)
    assert body["total"]["beta_total"] == pytest.approx(main["slope_per_point"], abs=TOL)
    assert body["component_count"] == 4 and body["components"][0]["title"] == "Fed cuts rates?"
    assert "attribution of 'econ' vs AAPL" in caplog.text and "4 components" in caplog.text and " ms " in caplog.text


def test_attribution_preview_unsaved_rule_and_errors(env):
    preview = env.post("/indices/draft/correlation/AAPL/attribution/preview", json={"categories": ["economics"]}).json()
    assert preview["index_name"] == "draft" and preview["component_count"] == 4 and preview["total"] is not None
    empty = env.post("/indices/econ/correlation/AAPL/attribution/preview", json={"categories": ["sports"]}).json()
    assert empty["index_days"] == 0 and empty["components"] == []
    no_prices = env.get("/indices/econ/correlation/MSFT/attribution").json()
    assert no_prices["n"] == 0 and no_prices["components"] == []
    assert env.get("/indices/econ/correlation/XXXX/attribution").status_code == 404
    assert env.get("/indices/nope/correlation/AAPL/attribution").status_code == 404
    assert env.get("/indices/draft/correlation/AAPL/attribution").status_code == 404  # preview did not store the rule


def test_frontend_has_attribution_section():
    html = (STATIC / "index.html").read_text()
    js = (STATIC / "attribution.js").read_text()
    assert 'id="cr-attr-rows"' in html and "Beitrag je Komponente" in html
    assert html.index("trades.js") < html.index("attribution.js")
    assert "loadAttribution(" in (STATIC / "trades.js").read_text()
    assert "/attribution" in js and "logEvent" in js and "Übrige" in js and "keine p-Werte" in js
