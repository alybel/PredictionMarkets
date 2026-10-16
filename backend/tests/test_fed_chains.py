"""Fed rate chains, derived layer (1015-2043): expected rate change, normalization, combined series, front and second
chain with rolling and chain linking, checks, data-quality endpoints. No network."""

from datetime import date, datetime, time, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from backend.app.api import app, get_session
from backend.app.fed.api import get_fed_source
from backend.app.fed.checks import check_divergence, check_jumps, check_missing, check_prob_sum
from backend.app.fed.loader import load_fed
from backend.app.fed.metrics import (COMBINED, RawPoint, SessionValue, build_chain, build_chains, combine,
                                     expected_change, session_values)
from backend.app.fed.sources import HttpFedSource
from backend.tests.eui_helpers import memory_session
from backend.tests.fed_fixtures import DAYS, NOW, SESSION, FakeApi

A, B, C = date(2026, 1, 28), date(2026, 3, 18), date(2026, 4, 29)


def pt(source, session_date, bp, day, price, hour=4):
    return RawPoint(source, session_date, bp, f"{bp:+d} bp", f"{source}-{session_date}-{bp}", day, price, None,
                    datetime.combine(day, time(hour), tzinfo=timezone.utc), "historical")


def session_points(source, session_date, day, probs: dict[int, float], hour=4):
    return [pt(source, session_date, bp, day, p, hour) for bp, p in probs.items()]


# ---- expected change and normalization ------------------------------------------------------

def test_expected_change_example():
    value, raw = expected_change([(0.70, 0), (0.25, -25), (0.05, -50)])
    assert value == pytest.approx(-8.75) and raw == pytest.approx(1.0)


def test_raw_sum_of_108_percent_is_normalized_and_kept():
    accepted, _ = session_values(session_points("kalshi", A, A, {0: 0.756, -25: 0.27, -50: 0.054}))
    v = accepted[("kalshi", A, A)]
    assert v.raw_sum == pytest.approx(1.08) and v.expected_bp == pytest.approx(-8.75)


def test_incomplete_days_and_prices_after_the_decision_are_rejected():
    points = (session_points("polymarket", A, A - timedelta(1), {0: 0.0015, 25: 0.242})       # main outcome missing
              + session_points("kalshi", A, A, {0: 0.9, -25: 0.1}, hour=19)                    # after the 18:00 UTC decision
              + session_points("kalshi", A, A - timedelta(1), {0: 0.9}))                       # a single outcome
    accepted, rejected = session_values(points)
    assert accepted == {} and ("polymarket", A, A - timedelta(1)) in rejected and ("kalshi", A, A - timedelta(1)) in rejected


def test_combined_is_the_mean_of_both_sources_or_the_single_one():
    values = {("kalshi", A, A): SessionValue(-10.0, 1.0, 3, ["kalshi"]), ("polymarket", A, A): SessionValue(-6.0, 1.02, 4, ["polymarket"]),
              ("kalshi", A, A + timedelta(1)): SessionValue(-4.0, 1.0, 3, ["kalshi"])}
    out = combine(values)
    assert out[(COMBINED, A, A)].expected_bp == pytest.approx(-8.0) and out[(COMBINED, A, A)].sources == ["kalshi", "polymarket"]
    assert out[(COMBINED, A, A + timedelta(1))].expected_bp == pytest.approx(-4.0)


# ---- chains ---------------------------------------------------------------------------------

def chain_values():
    """Kalshi values for meetings A, B, C on the two days around the decision of A and a later one."""
    v = lambda x: SessionValue(x, 1.0, 3, ["kalshi"])  # noqa: E731
    return {("kalshi", A, A): v(-20.0), ("kalshi", B, A): v(-5.0), ("kalshi", C, A): v(-2.0),
            ("kalshi", B, A + timedelta(1)): v(-7.0), ("kalshi", C, A + timedelta(1)): v(-3.0)}


def test_front_chain_rolls_from_a_to_b_after_the_decision():
    front = build_chain(chain_values(), "kalshi", [A, B, C], 0)
    assert [(p.day, p.session_date, p.roll) for p in front] == [(A, A, False), (A + timedelta(1), B, True)]


def test_second_chain_rolls_from_b_to_c_after_the_decision():
    second = build_chain(chain_values(), "kalshi", [A, B, C], 1)
    assert [(p.day, p.session_date, p.roll) for p in second] == [(A, B, False), (A + timedelta(1), C, True)]


def test_roll_day_change_is_measured_within_the_new_market():
    front = build_chain(chain_values(), "kalshi", [A, B, C], 0)
    roll = front[1]
    assert roll.value == pytest.approx(-7.0)
    assert roll.change == pytest.approx(-2.0)  # B: -7 vs. its own -5 the day before, not -7 vs. A's -20
    assert roll.linked == pytest.approx(-2.0)  # the jump-free level only moves by the within-market change
    second = build_chain(chain_values(), "kalshi", [A, B, C], 1)
    assert second[1].change == pytest.approx(-1.0)


# ---- checks ---------------------------------------------------------------------------------

def test_checks_report_prob_sum_missing_day_divergence_and_jump():
    v = lambda x, raw=1.0, src="kalshi": SessionValue(x, raw, 3, [src])  # noqa: E731
    values = {("kalshi", B, A): v(-5.0, 1.25), ("kalshi", B, A + timedelta(2)): v(-30.0),
              ("polymarket", B, A): v(-25.0, 1.0, "polymarket")}
    assert check_prob_sum(values, {})["count"] == 1
    missing = check_missing(values)
    assert missing["count"] == 1 and missing["examples"][0]["von"] == (A + timedelta(1)).isoformat()
    assert check_divergence(values)["examples"][0]["abstand_bp"] == pytest.approx(20.0)
    chains = build_chains(combine({("kalshi", B, A): v(-5.0), ("kalshi", B, A + timedelta(1)): v(-30.0)}), [B])
    jumps = check_jumps(chains)
    assert jumps["status"] == "warn" and {(e["reihe"], e["kette"]) for e in jumps["examples"]} == {("kalshi", "front"), ("combined", "front")}
    assert all(c["status"] == "ok" for c in (check_prob_sum({("kalshi", B, A): v(-5.0, 1.05)}, {}), check_missing({}),
                                             check_divergence({}), check_jumps({})))


# ---- endpoints -------------------------------------------------------------------------------

@pytest.fixture()
def client():
    session = memory_session()
    load_fed(session, HttpFedSource(FakeApi()), now=NOW)
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_fed_source] = lambda: HttpFedSource(FakeApi())
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_quality_endpoints_deliver_coverage_chains_checks_and_drill_down(client):
    cov = client.get("/quality/fed/coverage").json()
    assert cov["first_date"] == DAYS[0].isoformat() and cov["sessions"] == [SESSION.isoformat()]
    assert [(r["source"], r["days"], r["gaps"]) for r in cov["rows"]] == [("kalshi", 2, []), ("polymarket", 2, [])]

    chains = client.get("/quality/fed/chains").json()
    assert set(chains["series"]) == {"kalshi", "polymarket", "combined"} and set(chains["rolls"]) == set(chains["series"])
    front = chains["series"]["kalshi"]["front"]
    assert [p["date"] for p in front] == [d.isoformat() for d in DAYS] and front[0]["value"] == pytest.approx(-8.75)
    assert set(front[0]) == {"date", "session_date", "value", "change", "roll", "linked"}
    assert chains["series"]["combined"]["front"][0]["value"] == pytest.approx((-8.75 - 10.5) / 2)

    checks = client.get("/quality/fed/checks").json()
    assert [c["key"] for c in checks] == ["prob_sum", "missing_days", "divergence", "jumps", "after_close", "after_decision"]
    assert next(c for c in checks if c["key"] == "prob_sum")["count"] == 0  # 108 % lies inside the 10 % tolerance
    assert next(c for c in checks if c["key"] == "after_close")["status"] == "ok"

    day = client.get(f"/quality/fed/day/{DAYS[1].isoformat()}").json()
    assert day["front_session"] == SESSION.isoformat() and day["sessions"][0]["chain"] == "front"
    kalshi = day["sessions"][0]["sources"]["kalshi"]
    assert kalshi["raw_sum"] == pytest.approx(1.08) and [o["bp"] for o in kalshi["outcomes"]] == [-50, -25, 0, 25]
    assert set(kalshi["outcomes"][0]) == {"external_id", "label", "bp", "price", "probability", "volume_usd", "observed_at", "origin", "used"}
    assert client.get("/quality/fed/day/2020-01-01").status_code == 404


def test_load_endpoint_is_idempotent(client):
    summary = client.post("/fed/load").json()
    assert summary["rows_added"] == 0 and summary["markets_listed"] == 8 and summary["markets_failed"] == 0


def test_existing_quality_and_index_endpoints_still_answer(client):
    assert client.get("/quality/report").status_code == 200
    assert client.get("/indices").json() == []
    assert client.get("/history/status").status_code == 200
