"""Forecast report endpoints and triggers (risk index 6/7): full structure, 404, report on save and in the daily run,
point in time (nothing after the cutoff, provisional closes blocked), top-100 evaluation unchanged. No network, no LLM."""

import logging
from datetime import datetime, time, timedelta, timezone

from backend.app.eui.models import MarketSnapshot
from backend.app.forecast.models import ForecastReport
from backend.app.forecast.report import compute_report, daily_forecast
from backend.app.forecast.targets import TICKERS
from backend.app.stocks.api import get_fetcher
from backend.app.stocks.metric_data import find_index
from backend.app.models import utcnow
from backend.app.stocks.models import StockPrice
from backend.tests.test_metric_regression import _closes, env  # noqa: F401  (fixture)

NAME = "Fed Rates"


def _benchmarks(session, days):
    for i, ticker in enumerate(TICKERS):
        rets = {d: 0.002 * ((j * (i + 3)) % 5 - 2) for j, d in enumerate(days)}
        session.add_all(StockPrice(ticker=ticker, price_date=d, close=c) for d, c in _closes(100.0 + i, rets, days).items())
    session.commit()


def _days(inputs):
    return sorted(inputs.values)


def test_endpoint_returns_the_full_structure_with_short_history_hint(env, caplog):  # noqa: F811
    client, session, inputs = env
    _benchmarks(session, _days(inputs))
    with caplog.at_level(logging.INFO):
        body = client.get(f"/builder/indices/{NAME}/forecast").json()
    assert body["index_name"] == NAME and body["horizons"] == [1, 5, 20] and body["trigger"] == "on_demand"
    assert body["short_history"] and any("unter 3 Jahren" in w for w in body["warnings"])
    assert [t["ticker"] for t in body["targets"]] == ["^GSPC", "^TNX", "TLT", "^VIX"]
    for t in body["targets"]:
        assert {"score", "light", "light_label", "explanation", "passed", "checks", "closes"} <= set(t) and t["explanation"]
        assert [h["horizon"] for h in t["horizons"]] == [1, 5, 20]
        assert {"correlation", "forecast_correlation", "p_value", "oos_r2", "slope", "folds", "passed"} <= set(t["horizons"][0])
        assert (t["crash"] is not None) == (t["ticker"] in ("^GSPC", "TLT"))
    spx = body["targets"][0]
    assert {"hit_rate", "base_rate", "lift", "recall", "signals"} <= set(spx["crash"]) and spx["checks"] == 4
    assert "forecast run: index='Fed Rates' target=^GSPC horizon=5 index_days=" in caplog.text
    assert client.get(f"/builder/indices/{NAME}/forecast").json()["computed_at"] == body["computed_at"]  # stored


def test_unknown_index_gives_404(env):  # noqa: F811
    client, _, _ = env
    assert client.get("/builder/indices/nope/forecast").status_code == 404
    assert client.post("/builder/indices/nope/forecast").status_code == 404


def test_report_is_built_on_save_and_removed_on_delete(env):  # noqa: F811
    client, session, _ = env
    assert client.post("/builder/indices", json={"name": "Neu", "components": [{"key": "b_one"}]}).status_code == 201
    report = session.query(ForecastReport).filter_by(index_name="Neu").one()
    assert report.trigger == "save" and report.payload["index_name"] == "Neu"
    first = report.payload["computed_at"]
    assert client.put("/builder/indices/Neu", json={"components": [{"key": "b_two"}]}).status_code == 200
    session.expire_all()
    assert session.query(ForecastReport).filter_by(index_name="Neu").one().payload["computed_at"] >= first
    assert client.delete("/builder/indices/Neu").status_code == 204
    assert session.query(ForecastReport).filter_by(index_name="Neu").count() == 0


def test_daily_run_loads_target_closes_and_rebuilds_every_report(env):  # noqa: F811
    _, session, inputs = env
    days = _days(inputs)
    asked = []

    def fetcher(ticker, start):
        asked.append(ticker)
        return {d: 100.0 + i for i, d in enumerate(days)}

    summary = daily_forecast(session, fetcher)
    assert sorted(asked) == sorted(TICKERS) and summary["prices"]["rows_added"] == 4 * len(days)
    assert summary["reports"] == summary["indices"] == 1
    assert session.query(ForecastReport).filter_by(index_name=NAME).one().trigger == "daily"

    def broken(ticker, start):
        raise OSError("offline")

    assert daily_forecast(session, broken)["reports"] == 1  # a failing price load keeps the stored closes


def test_recompute_with_price_load_uses_the_injected_fetcher(env):  # noqa: F811
    client, _, inputs = env
    days = _days(inputs)
    client.app.dependency_overrides[get_fetcher] = lambda: lambda ticker, start: {d: 50.0 for d in days}
    body = client.post(f"/builder/indices/{NAME}/forecast?load_prices=true").json()
    assert body["trigger"] == "manual" and all(t["closes"] == len(days) for t in body["targets"])


def test_nothing_after_the_cutoff_and_no_provisional_close_enters(env):  # noqa: F811
    _, session, inputs = env
    days = _days(inputs)
    _benchmarks(session, days)
    row = find_index(session, NAME)
    cutoff = days[-15]
    before = compute_report(session, row, cutoff)
    assert before["last_date"] <= cutoff.isoformat()
    # changed index inputs and closes after the cutoff, a future-dated close and a provisional (intraday) close
    # must not change the report as of the cutoff
    session.query(MarketSnapshot).filter(MarketSnapshot.snapshot_date > cutoff).update({"price": 0.99})
    session.query(StockPrice).filter(StockPrice.price_date > cutoff).update({"close": 1.0})
    session.add(StockPrice(ticker="^GSPC", price_date=utcnow().date() + timedelta(3), close=1.0))
    session.query(StockPrice).filter_by(ticker="^VIX", price_date=cutoff).update(
        {"close": 999.0, "fetched_at": datetime.combine(cutoff, time(12), tzinfo=timezone.utc)})
    session.commit()
    after = compute_report(session, row, cutoff)
    vix_before, vix_after = before["targets"][3], after["targets"][3]
    assert vix_after["closes"] == vix_before["closes"] - 1                       # provisional close blocked
    assert after["targets"][:3] == before["targets"][:3] and after["index_days"] == before["index_days"]
    assert compute_report(session, row)["targets"][0] != before["targets"][0]  # without the cutoff the change shows


def test_top_100_evaluation_is_unchanged(env):  # noqa: F811
    client, session, inputs = env
    urls = [f"/builder/indices/{NAME}/correlation?days=365&target=raw", f"/builder/indices/{NAME}/correlation?days=365",
            f"/builder/indices/{NAME}/correlation/AAPL?days=365&target=raw", "/stocks/status"]
    before = [client.get(u).json() for u in urls]
    _benchmarks(session, _days(inputs))
    assert client.post(f"/builder/indices/{NAME}/forecast").status_code == 200
    assert [client.get(u).json() for u in urls] == before
    assert before[3]["tickers"] == 3
