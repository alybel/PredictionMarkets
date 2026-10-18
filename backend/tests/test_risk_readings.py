"""Risk readings "Unsicherheit" and "Volatilität" in the fixed rule catalog (1017-1107 3/7): uncertainty 1 - 2|p - 0.5|,
volatility = sample standard deviation of the last 20 within-link daily changes (no roll jump, gaps stay empty, a short
window gives no value), reading stored per link and shown in the metric catalog; chain and basket metrics keep their
values. No network, no LLM."""

import logging
from datetime import date, timedelta
from statistics import stdev

from fastapi.testclient import TestClient

from backend.app.api import app, get_session
from backend.app.fed.api import get_fed_source
from backend.app.metrics import chain_rules
from backend.app.metrics.basket import basket_points
from backend.app.metrics.catalog import ChainLink, basket_def
from backend.app.metrics.chain import link_series
from backend.app.metrics.models import CustomMetric
from backend.app.metrics.service import create_basket, create_structured
from backend.tests.eui_helpers import memory_session
from backend.tests.test_metric_catalog import EmptySource
from backend.tests.test_metric_catalog_ui import metric, run
from backend.tests.test_metric_index_builder import days, snapshots

D = date(2026, 1, 1)
RULES = (chain_rules.DIRECTION, chain_rules.ROLL_END_DATE, chain_rules.NO_GAP_FILL, chain_rules.NO_FORWARD_FILL)
W = chain_rules.VOL_WINDOW


def day(n: int) -> date:
    return D + timedelta(days=n)


def prices(**series) -> dict:
    return {("kalshi", k): {day(n): (p, 100.0) for n, p in s.items()} for k, s in series.items()}


def link(key: str, roll: int | None, reading: str) -> ChainLink:
    return ChainLink("kalshi", key, key.upper(), day(roll) if roll is not None else None, reading)


def test_readings_are_in_the_catalog():
    assert list(chain_rules.READINGS) == ["up", "down", "uncertainty", "volatility"]
    assert chain_rules.reading("invented") == "up" and chain_rules.reading(None) == "up"


def test_uncertainty_peaks_at_one_half():
    pts = link_series([link("a", None, "uncertainty")], RULES, prices(a={0: 0.5, 1: 0.1, 2: 0.9}))
    assert [round(p.level, 9) for p in pts] == [1.0, 0.2, 0.2]
    assert round(pts[1].change, 9) == -0.8 and round(pts[2].change, 9) == 0.0


def test_volatility_of_constant_change_is_zero():
    pts = link_series([link("a", None, "volatility")], RULES, prices(a={n: 0.1 + 0.01 * n for n in range(W + 5)}))
    assert len(pts) == 5 and all(abs(p.value) < 1e-12 for p in pts)


def test_volatility_of_a_known_series():
    steps = [0.01 if n % 2 else -0.02 for n in range(W)]
    series = {0: 0.5}
    for n, s in enumerate(steps, 1):
        series[n] = series[n - 1] + s
    pts = link_series([link("a", None, "volatility")], RULES, prices(a=series))
    assert len(pts) == 1 and pts[0].day == day(W)
    assert abs(pts[0].value - stdev(steps)) < 1e-12 and abs(pts[0].value - pts[0].level) < 1e-12


def test_volatility_ignores_the_roll_jump():
    a = {n: 0.2 + 0.01 * n for n in range(W)}           # constant change inside A
    b = {n: 0.9 + 0.01 * (n - W) for n in range(W - 1, 2 * W)}  # B starts much higher, same constant change
    pts = link_series([link("a", W - 1, "volatility"), link("b", 3 * W, "volatility")], RULES, prices(a=a, b=b))
    assert pts and all(abs(p.value) < 1e-12 for p in pts)  # a 0.7 jump would show up as volatility
    assert pts[0].day == day(W) and pts[0].roll                    # first full window lies on the roll day


def test_gaps_stay_empty_and_a_short_window_gives_no_value():
    series = {n: 0.3 + (0.02 if n % 2 else 0.0) for n in range(W + 3)}
    series.pop(W + 1)
    pts = link_series([link("a", None, "volatility")], RULES, prices(a=series))
    assert day(W + 1) not in {p.day for p in pts}                    # no forward fill on a missing day
    assert [p.day for p in pts] == [day(W), day(W + 2)]
    assert not link_series([link("a", None, "volatility")], RULES, prices(a={n: 0.3 for n in range(W)}))  # 19 changes: too short


def test_existing_chain_and_basket_values_unchanged():
    data = prices(a={0: 0.2, 1: 0.3, 2: 0.4}, b={0: 0.6, 1: 0.6, 2: 0.6, 3: 0.7, 4: 0.8})
    pts = link_series([link("b", 10, "up"), link("a", 2, "up")], RULES, data)
    assert [round(p.value, 6) for p in pts] == [0.2, 0.3, 0.4, 0.5, 0.6] and [p.roll for p in pts] == [False, False, False, True, False]
    down = link_series([link("a", None, "down")], RULES, prices(a={0: 0.3, 1: 0.4}))
    assert [round(p.value, 6) for p in down] == [0.7, 0.6]
    session = memory_session()
    snapshots(session, lambda i: 0.2 + 0.01 * i)
    create_basket(session, "cm_korb", "Korb", "", [{"source": "polymarket", "external_id": "pm-x", "title": "Recession?"}])
    values = [round(p.value, 6) for p in basket_points(session, basket_def(session.get(CustomMetric, "cm_korb")), days()[-1])[0]]
    assert values == [round(0.2 + 0.01 * i, 6) for i in range(len(days()))]


def test_reading_is_stored_and_shown_in_the_catalog(caplog):
    session = memory_session()
    snapshots(session, lambda i: 0.2 + 0.01 * i)
    link_spec = {"source": "polymarket", "external_id": "pm-x", "title": "Recession?", "roll_on": days()[-1].isoformat(), "direction": "volatility"}
    with caplog.at_level(logging.INFO):
        key, _, ev = create_structured(session, "Rezessions-Volatilität", "single", {"links": [link_spec], "rules": ["direction"]})
    assert session.get(CustomMetric, key).spec["links"][0]["direction"] == "volatility"
    assert len(ev.points) == max(0, len(days()) - W) and all(abs(p.value) < 1e-9 for p in ev.points)  # short history: window rule
    assert "Lesart: Volatilität" in ev.defn.calculation and "reading(s) {'volatility': 1}" in caplog.text
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_fed_source] = EmptySource
    try:
        metrics = {m["key"]: m for m in TestClient(app).get("/metrics").json()["metrics"]}
    finally:
        app.dependency_overrides.clear()
    assert metrics[key]["reading"] == "volatility" and metrics[key]["reading_label"] == "Volatilität"
    assert metrics[key]["links"][0]["reading_label"] == "Volatilität"


def test_metric_card_shows_the_reading():
    texts = run(metric(unit="probability", reading="volatility", reading_label="Volatilität"))["texts"]
    assert any("Lesart: Volatilität" in t for t in texts)
    assert not any("Lesart" in t for t in run(metric())["texts"])     # Fed chains have no reading line
