"""Forecast power statistics (risk index 6/7): planted 5-day lead is found, pure noise is not, walk-forward never
trains on test or later data, crash flags and hit rate on constructed series, no pairs across data gaps. Pure, no I/O."""

import random
from datetime import date, timedelta

import pytest

from backend.app.forecast.crash import crash_flags, crash_observations, evaluate_crash
from backend.app.forecast.report import build_report, target_report
from backend.app.forecast.targets import TARGETS
from backend.app.forecast.walkforward import evaluate, folds, observations, runs
from backend.app.stocks.correlation import MAX_GAP_DAYS

SPX = TARGETS[0]


def weekdays(start: date, n: int) -> list[date]:
    out, day = [], start
    while len(out) < n:
        if day.weekday() < 5:
            out.append(day)
        day += timedelta(1)
    return out


def lead_series(n: int = 800, lead: int = 5, seed: int = 1, strength: float = 0.004):
    """Index random walk; the stock moves ``lead`` days after the index (strength 0 = pure noise)."""
    rnd = random.Random(seed)
    days = weekdays(date(2023, 1, 2), n)
    steps = [rnd.gauss(0, 1) for _ in days]
    index, closes, level, close = {}, {}, 0.0, 100.0
    for i, day in enumerate(days):
        level += steps[i]
        close *= 1 + (strength * steps[i - lead] if i >= lead else 0.0) + rnd.gauss(0, 0.004)
        index[day], closes[day] = level, close
    return index, closes


def test_planted_five_day_lead_is_found():
    index, closes = lead_series()
    report = target_report(SPX, index, closes)
    by_h = {h["horizon"]: h for h in report["horizons"]}
    assert by_h[5]["passed"] and by_h[5]["forecast_correlation"] > 0.5 and by_h[5]["oos_r2"] > 0.2
    assert not by_h[1]["passed"]  # the next day carries no information with a 5-day lead
    assert max(by_h.values(), key=lambda h: h["forecast_correlation"])["horizon"] == 5
    assert report["light"] in ("green", "yellow") and report["score"] > 0 and "5 Handelstage" in report["explanation"]


@pytest.mark.parametrize("seed", [2, 3, 4])
def test_pure_noise_shows_no_significant_forecast_power(seed):
    index, closes = lead_series(seed=seed, strength=0.0)
    report = target_report(SPX, index, closes)
    assert report["passed"] == 0 and report["light"] == "red" and report["score"] == 0
    assert "Keine belastbare Prognosekraft" in report["explanation"]


def test_walk_forward_trains_only_on_outcomes_before_each_test_block():
    index, closes = lead_series(n=300)
    obs = observations(index, closes, 5)
    pairs = list(folds(obs))
    assert len(pairs) == 3
    for train, block in pairs:
        assert train and all(o.end < block[0].day for o in train)          # nothing from the test block or later
        assert all(o.day < block[0].day for o in train)
    tested = [o for _, block in pairs for o in block]
    assert tested == obs[len(obs) // 2:]                                       # first half only trains
    # changing outcomes of the last block never changes the fit used to predict it
    before = evaluate(obs, 5)["folds"]
    last = pairs[-1][1][0].day
    shuffled = [o if o.day < last else type(o)(o.day, o.end, o.x, -o.y * 7) for o in obs]
    after = evaluate(shuffled, 5)["folds"]
    assert [f["slope"] for f in after] == [f["slope"] for f in before]
    assert all(f["train_to"] < f["test_from"] for f in after)


def test_no_pair_across_data_gaps():
    first = weekdays(date(2024, 1, 1), 12)
    second = weekdays(first[-1] + timedelta(MAX_GAP_DAYS + 3), 12)
    days = first + second
    index = {d: float(i * i % 7) for i, d in enumerate(days)}
    closes = {d: 100.0 + i for i, d in enumerate(days)}
    assert [len(r) for r in runs(days)] == [12, 12]
    assert len(observations(index, closes, 1)) == 20 and len(observations(index, closes, 5)) == 4
    for o in observations(index, closes, 5):
        assert (o.day in first) == (o.end in first)  # never from one run into the other
    assert set(crash_flags(closes)) <= set(days) and not any(d in first[-5:] for d in crash_flags(closes))


def test_crash_flags_on_a_constructed_series():
    days = [date(2024, 1, 1) + timedelta(i) for i in range(100)]
    closes = {d: 100.0 if i < 50 else 89.0 for i, d in enumerate(days)}
    flags = crash_flags(closes)
    assert sorted(flags) == days[:70]                                            # window of the last 30 days incomplete
    assert [d for d, (_, crash) in flags.items() if crash] == days[20:50]
    assert flags[days[0]][0] == days[30]
    exactly_ten = {d: 100.0 if i < 50 else 90.0 for i, d in enumerate(days)}
    assert not any(crash for _, crash in crash_flags(exactly_ten).values())     # a fall of exactly 10 % is no crash


def test_crash_hit_rate_on_known_falls():
    days = [date(2021, 1, 1) + timedelta(i) for i in range(1000)]
    closes, levels = {}, {}
    for i, d in enumerate(days):
        phase = i % 100                                            # fall to 85 on day 70 of every cycle, back on day 80
        closes[d] = 85.0 if 70 <= phase < 80 else 100.0
        levels[d] = 1.0 if 45 <= phase < 70 else 0.0               # index high in the 25 days before each fall
    result = evaluate_crash(crash_observations(levels, closes))
    assert result["hit_rate"] == 1.0 and result["base_rate"] == pytest.approx(0.3, abs=0.02)
    assert result["lift"] == pytest.approx(1 / result["base_rate"]) and result["recall"] == pytest.approx(25 / 30, abs=0.02)
    assert result["enough"] and result["p_value"] < 0.01
    assert all(f["side"] == "hoch" for f in result["folds"])


def test_short_history_warning_and_manual_weights_note():
    index, closes = lead_series(n=300)
    report = build_report("x", index, {SPX.ticker: closes}, max(index))
    assert report["short_history"] and "unter 3 Jahren" in report["warnings"][0]
    assert any("manuell" in w for w in report["warnings"])
    assert [t["ticker"] for t in report["targets"]] == ["^GSPC", "^TNX", "TLT", "^VIX"]
    assert report["targets"][1]["light"] == "grey" and "Keine Kurse" in report["targets"][1]["explanation"]
    long_index, long_closes = lead_series(n=820)
    assert not build_report("x", long_index, {}, max(long_index), "equal")["short_history"]
