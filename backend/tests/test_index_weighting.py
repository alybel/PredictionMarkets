"""Weighting methods and AI weighting proposal of the index builder (risk index 5/7). No network, no LLM (fake weighers)."""

import logging
import math
from datetime import date, timedelta

import pytest

from backend.app.builder.api import get_weigher
from backend.app.builder.proposal import RuleWeigher
from backend.app.builder.service import index_series, standardize
from backend.app.builder.weighting import EQUAL, INVERSE_VOL, LIQUIDITY, MANUAL, Weighting, percent, weighted_series
from backend.app.api import app
from backend.app.metrics.catalog import MetricPoint
from backend.app.metrics.service import evaluate_db
from backend.tests.test_metric_index_builder import BASKET, FED, env, two_metric_index  # noqa: F401 (fixture)

START = date(2025, 1, 1)


def series(values, liquidity=None):
    return [MetricPoint(START + timedelta(i), v, START, None, False, liquidity(i) if liquidity else None) for i, v in enumerate(values)]


def weighting(method, points, manual=None):
    return Weighting(method, points, {k: standardize(p) for k, p in points.items()}, manual)


def test_equal_weights_give_a_third_each_and_sum_to_100():
    points = {k: series([i * (n + 1) for i in range(20)]) for n, k in enumerate("abc")}
    w = weighting(EQUAL, points).at(START + timedelta(19))
    assert w == pytest.approx({"a": 1 / 3, "b": 1 / 3, "c": 1 / 3})
    pct = percent(w)
    assert sum(pct.values()) == pytest.approx(100.0) and sorted(pct.values()) == [33.3, 33.3, 33.4]


def test_inverse_volatility_gives_the_calmer_metric_more_weight():
    calm = series([math.sin(i / 10) for i in range(120)])                 # smooth wave: small daily z changes
    wild = series([(-1) ** i * (1 + i % 7) for i in range(120)])           # zig-zag: large daily z changes
    w = weighting(INVERSE_VOL, {"calm": calm, "wild": wild}).at(START + timedelta(119))
    assert w["calm"] > w["wild"] and sum(w.values()) == pytest.approx(1.0)


def test_liquidity_weights_are_proportional_to_turnover():
    points = {"a": series([1.0] * 40, lambda i: 10_000.0), "b": series([2.0] * 40, lambda i: 30_000.0)}
    w = weighting(LIQUIDITY, points).at(START + timedelta(39))
    assert w == pytest.approx({"a": 0.25, "b": 0.75})
    assert percent(w) == {"a": 25.0, "b": 75.0}


@pytest.mark.parametrize("method", [LIQUIDITY, INVERSE_VOL])
def test_weights_ignore_data_after_the_cutoff_day(method):
    cutoff, n = START + timedelta(59), 60
    base = {"a": [math.sin(i / 8) for i in range(n)], "b": [(-1) ** i * (i % 5) for i in range(n)]}
    short = {"a": series(base["a"], lambda i: 5_000.0), "b": series(base["b"], lambda i: 20_000.0)}
    long = {"a": series(base["a"] + [(-1) ** i * 50 for i in range(60)], lambda i: 5_000.0 if i < n else 900_000.0),
            "b": series(base["b"] + [0.0] * 60, lambda i: 20_000.0 if i < n else 1.0)}
    assert weighting(method, short).at(cutoff) == pytest.approx(weighting(method, long).at(cutoff))
    assert weighting(method, short).at(cutoff) != pytest.approx(weighting(method, long).at(START + timedelta(119)))


def test_missing_data_falls_back_to_equal_weights():
    w = weighting(LIQUIDITY, {"a": series([1.0] * 10), "b": series([2.0] * 10, lambda i: 1.0)})
    assert w.at(START + timedelta(9)) == pytest.approx({"a": 0.5, "b": 0.5}) and w.fallback_days == 1


def test_manual_method_reproduces_the_existing_index_series():
    points = {"a": series([i % 4 for i in range(30)]), "b": series([i * 0.3 for i in range(30)])}
    z = {k: standardize(p) for k, p in points.items()}
    old = index_series(z, {"a": 3, "b": 1})
    assert weighted_series(z, weighting(MANUAL, points, {"a": 3, "b": 1}).at) == old


# ---- API: saved method, regression, proposal --------------------------------------------------------------------


def test_existing_index_keeps_its_values(env):
    client, session = env
    assert two_metric_index(client).status_code == 201
    body = client.get("/builder/indices/Makro").json()
    evaluations = evaluate_db(session)
    expected = index_series({k: standardize(evaluations[k].points) for k in (FED, BASKET)}, {FED: 3, BASKET: 1})
    assert body["weighting"] == MANUAL and body["points"] == expected[-365:]
    assert {c["key"]: c["weight_pct"] for c in body["components"]} == {FED: 75.0, BASKET: 25.0}


@pytest.mark.parametrize("method", [EQUAL, LIQUIDITY, INVERSE_VOL, MANUAL])
def test_every_method_is_selectable_and_sums_to_100(env, method, caplog):
    client, _ = env
    caplog.set_level(logging.INFO)
    comps = [{"key": FED, "weight": 3}, {"key": BASKET, "weight": 1}]
    assert client.post("/builder/indices", json={"name": "W", "components": comps, "weighting": method}).status_code == 201
    body = client.get("/builder/indices/W").json()
    assert body["weighting"] == method and body["points"]
    assert sum(c["weight_pct"] for c in body["components"]) == pytest.approx(100.0)
    preview = client.post("/builder/weighting/preview", json={"keys": [FED, BASKET], "method": method}).json()
    assert sum(preview["weights_pct"].values()) == pytest.approx(100.0)
    assert f"weighting {method}" in caplog.text
    assert client.get("/builder/metrics").json()["weighting_methods"][method]


def test_unknown_method_is_refused(env):
    client, _ = env
    comps = [{"key": FED, "weight": 1}]
    assert client.post("/builder/indices", json={"name": "X", "components": comps, "weighting": "magic"}).status_code == 422


class FakeWeigher:
    name = "fake-sonnet"

    def __init__(self, answer):
        self.answer, self.calls = answer, []

    def propose(self, ctx):
        self.calls.append(ctx)
        return self.answer


def with_weigher(answer):
    fake = FakeWeigher(answer)
    app.dependency_overrides[get_weigher] = lambda: fake
    return fake


def test_proposal_shows_method_weights_and_rationale_and_stores_nothing(env, caplog):
    client, session = env
    caplog.set_level(logging.INFO)
    fake = with_weigher({"method": MANUAL, "weights": [{"key": FED, "weight": 1}, {"key": BASKET, "weight": 3}],
                         "rationale": "Der Korb ist liquider."})
    res = client.post("/builder/weighting/propose", json={"keys": [FED, BASKET]})
    assert res.status_code == 200
    assert res.json() == {"method": MANUAL, "method_label": "Manuell", "weights_pct": {FED: 25.0, BASKET: 75.0},
                          "rationale": "Der Korb ist liquider.", "model": "fake-sonnet"}
    assert {c["key"] for c in fake.calls[0]} == {FED, BASKET} and "weights_pct_by_method" in fake.calls[0][0]
    assert client.get("/builder/indices").json()["indices"] == []          # nothing saved before confirmation
    assert "weighting proposal (fake-sonnet): method manual" in caplog.text


def test_data_driven_proposal_shows_the_computed_weights(env):
    client, _ = env
    with_weigher({"method": EQUAL, "weights": [{"key": FED, "weight": 90}, {"key": BASKET, "weight": 10}], "rationale": "x"})
    assert client.post("/builder/weighting/propose", json={"keys": [FED, BASKET]}).json()["weights_pct"] == {FED: 50.0, BASKET: 50.0}


def test_fake_proposal_with_unknown_metric_is_discarded_and_logged(env, caplog):
    client, _ = env
    with_weigher({"method": MANUAL, "weights": [{"key": FED, "weight": 1}, {"key": "invented_metric", "weight": 2}], "rationale": "x"})
    res = client.post("/builder/weighting/propose", json={"keys": [FED, BASKET]})
    assert res.status_code == 422 and "invented_metric" in res.json()["detail"]
    assert "discarded: unknown metrics ['invented_metric']" in caplog.text


def test_proposal_for_unvalidated_metric_is_refused(env):
    client, _ = env
    with_weigher({"method": EQUAL, "weights": [], "rationale": ""})
    assert client.post("/builder/weighting/propose", json={"keys": ["mf_draft"]}).status_code == 409


def test_confirmed_proposal_is_logged_on_save(env, caplog):
    client, _ = env
    caplog.set_level(logging.INFO)
    comps = [{"key": FED, "weight": 1}, {"key": BASKET, "weight": 1}]
    assert client.post("/builder/indices", json={"name": "K", "components": comps, "weighting": EQUAL, "proposal": "confirmed"}).status_code == 201
    assert client.put("/builder/indices/K", json={"components": comps, "weighting": INVERSE_VOL, "proposal": "changed"}).status_code == 200
    assert "AI proposal confirmed" in caplog.text and "AI proposal changed" in caplog.text and "weighting equal -> inverse_vol" in caplog.text


def test_rule_fallback_proposes_a_valid_method():
    ctx = [{"key": "a", "liquidity_usd": None, "z_volatility": 0.2, "weights_pct_by_method": {EQUAL: 50.0, LIQUIDITY: 50.0, INVERSE_VOL: 40.0}},
           {"key": "b", "liquidity_usd": 10.0, "z_volatility": 0.1, "weights_pct_by_method": {EQUAL: 50.0, LIQUIDITY: 50.0, INVERSE_VOL: 60.0}}]
    assert RuleWeigher().propose(ctx)["method"] == INVERSE_VOL


def test_builder_frontend_offers_methods_and_proposal_with_confirmation():
    from backend.tests.test_metric_index_builder import STATIC
    html, js = (STATIC / "index.html").read_text(), (STATIC / "miweights.js").read_text()
    for element in ('id="mi-weighting"', 'id="mi-propose"', 'id="mi-proposal-accept"', 'id="mi-proposal-reject"', 'id="mi-proposal-rationale"'):
        assert element in html
    assert '<script src="/static/miweights.js"></script>' in html
    assert '"/builder/weighting/propose"' in js and '"/builder/weighting/preview"' in js
    for event in ('"builder.weighting.proposal"', '"builder.weighting.proposal.confirmed"', '"builder.weighting.proposal.changed"',
                  '"builder.weighting.method"', '"builder.weighting.proposal.rejected"'):
        assert event in js
    assert "...weighting" in (STATIC / "metricindex.js").read_text()
