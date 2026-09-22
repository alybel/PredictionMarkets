"""Probe collector tests: batch fetch, USD fields, failure bookkeeping, run file.

No live API calls: the HTTP session is a fake keyed by URL.
"""

import json
import os
from datetime import datetime, timezone

import pytest
import requests

from backend.app.probe import collect as probe
from backend.app.probe.collect import (GAMMA_MARKETS_URL, KALSHI_MARKETS_URL, collect, collect_once, fetch_batch,
                                       load_config, main, write_record)

NOW = datetime(2026, 9, 22, 18, 0, tzinfo=timezone.utc)
CONFIG = {
    "kalshi": [{"id": "KXCPI-26SEP-T0.4", "topic": "cpi"}, {"id": "KXU3-26SEP-T3.9", "topic": "unemployment"}],
    "polymarket": [{"id": "2589812", "topic": "fed_rate"}],
}
KALSHI_PAYLOAD = {"markets": [
    {"ticker": "KXCPI-26SEP-T0.4", "title": "CPI above 0.4%?", "status": "active", "last_price": 89,
     "yes_bid": 88, "yes_ask": 90, "volume_24h": 1000, "open_interest_fp": "49594.69", "liquidity_dollars": "0",
     "close_time": "2026-10-14T12:25:00Z"},
    {"ticker": "KXU3-26SEP-T3.9", "title": "U-3 above 3.9%?", "status": "active", "last_price_dollars": "0.91",
     "yes_bid_dollars": "0.91", "yes_ask_dollars": "0.92", "volume_24h_fp": "200", "liquidity_dollars": "55000"},
]}
POLY_PAYLOAD = [{"id": 2589812, "question": "No change in Fed rates?", "outcomePrices": '["0.48", "0.52"]',
                 "liquidityNum": 318383.7, "volume24hr": 379423.5, "bestBid": 0.48, "bestAsk": 0.49,
                 "endDate": "2026-10-29T03:59:00Z", "active": True, "closed": False}]


class FakeResponse:
    def __init__(self, status_code=200, payload=None, invalid=False):
        self.status_code, self._payload, self._invalid = status_code, payload, invalid

    def json(self):
        if self._invalid:
            raise ValueError("bad json")
        return self._payload


class FakeSession:
    """Responses per URL; ``errors`` maps a URL to an exception to raise."""

    def __init__(self, responses: dict, errors: dict | None = None):
        self._responses, self._errors = responses, errors or {}
        self.calls: list[tuple[str, object]] = []

    def get(self, url, params=None, timeout=None, headers=None):
        self.calls.append((url, params))
        if url in self._errors:
            raise self._errors[url]
        return self._responses[url]


def healthy_session() -> FakeSession:
    return FakeSession({KALSHI_MARKETS_URL: FakeResponse(200, KALSHI_PAYLOAD), GAMMA_MARKETS_URL: FakeResponse(200, POLY_PAYLOAD)})


def test_two_batch_requests_and_usd_fields():
    session = healthy_session()
    record = collect(session, CONFIG, now=NOW, env={"GITHUB_RUN_ID": "77", "GITHUB_RUN_ATTEMPT": "1"})
    assert len(session.calls) == 2
    assert session.calls[0] == (KALSHI_MARKETS_URL, {"tickers": "KXCPI-26SEP-T0.4,KXU3-26SEP-T3.9"})
    assert session.calls[1] == (GAMMA_MARKETS_URL, [("id", "2589812")])
    assert record["status"] == "ok" and record["run_id"] == "77" and record["timestamp"] == "2026-09-22T18:00:00+00:00"
    assert record["sources"]["kalshi"] == {"http_status": 200, "elapsed_ms": record["sources"]["kalshi"]["elapsed_ms"],
                                           "error": None, "requested": 2, "received": 2}
    cpi, u3, fed = record["markets"]
    assert (cpi["platform"], cpi["market_id"], cpi["topic"]) == ("kalshi", "KXCPI-26SEP-T0.4", "cpi")
    assert cpi["price"] == pytest.approx(0.89) and cpi["spread"] == pytest.approx(0.02)
    assert cpi["volume_24h_usd"] == pytest.approx(1000 * 0.89)  # contracts x price
    assert cpi["depth_usd"] == pytest.approx(49594.69)  # open interest proxy when liquidity is 0
    assert cpi["end_date"] == "2026-10-14T12:25:00+00:00" and cpi["active"] is True and cpi["error"] is None
    assert u3["depth_usd"] == pytest.approx(55000) and u3["volume_24h_usd"] == pytest.approx(200 * 0.91)
    assert fed["price"] == pytest.approx(0.48) and fed["depth_usd"] == pytest.approx(318383.7)
    assert fed["volume_24h_usd"] == pytest.approx(379423.5) and fed["title"] == "No change in Fed rates?"


def test_missing_market_is_recorded_as_partial():
    session = FakeSession({KALSHI_MARKETS_URL: FakeResponse(200, {"markets": KALSHI_PAYLOAD["markets"][:1]}),
                           GAMMA_MARKETS_URL: FakeResponse(200, POLY_PAYLOAD)})
    record = collect(session, CONFIG, now=NOW, env={})
    assert record["status"] == "partial" and record["run_id"] is None
    missing = record["markets"][1]
    assert missing["market_id"] == "KXU3-26SEP-T3.9" and missing["error"] == "not_in_response" and missing["price"] is None
    assert record["sources"]["kalshi"]["received"] == 1


def test_rate_limit_is_logged_not_raised():
    session = FakeSession({KALSHI_MARKETS_URL: FakeResponse(429, {}), GAMMA_MARKETS_URL: FakeResponse(200, POLY_PAYLOAD)})
    record = collect(session, CONFIG, now=NOW, env={})
    assert record["status"] == "partial"
    assert record["sources"]["kalshi"] == {"http_status": 429, "elapsed_ms": record["sources"]["kalshi"]["elapsed_ms"],
                                           "error": "rate_limited", "requested": 2, "received": 0}
    assert all(m["error"] == "rate_limited" for m in record["markets"] if m["platform"] == "kalshi")


def test_all_sources_down_is_failed():
    session = FakeSession({GAMMA_MARKETS_URL: FakeResponse(500, None)},
                          errors={KALSHI_MARKETS_URL: requests.ConnectionError("down")})
    record = collect(session, CONFIG, now=NOW, env={})
    assert record["status"] == "failed"
    assert record["sources"]["kalshi"]["http_status"] is None and record["sources"]["kalshi"]["error"].startswith("request_error")
    assert record["sources"]["polymarket"]["error"] == "http_500"
    assert len(record["markets"]) == 3 and all(m["price"] is None for m in record["markets"])


def test_fetch_batch_handles_invalid_json_and_shape():
    invalid = fetch_batch(FakeSession({"u": FakeResponse(200, invalid=True)}), "u", {})
    assert invalid.http_status == 200 and invalid.error.startswith("invalid_json") and invalid.items == []
    odd = fetch_batch(FakeSession({"u": FakeResponse(200, {"markets": "nope"})}), "u", {})
    assert odd.error == "unexpected_payload"


def test_write_record_names_file_by_timestamp_and_run(tmp_path):
    record = collect(healthy_session(), CONFIG, now=NOW, env={"GITHUB_RUN_ID": "77"})
    path = write_record(record, tmp_path / "runs")
    assert path.name == "run-20260922T180000Z-77.json"
    assert json.loads(path.read_text())["markets"][0]["market_id"] == "KXCPI-26SEP-T0.4"
    assert write_record(collect(healthy_session(), CONFIG, now=NOW, env={}), tmp_path).name == "run-20260922T180000Z-local.json"


def test_repo_config_has_three_markets_per_platform():
    config = load_config()
    assert [len(config[p]) for p in ("kalshi", "polymarket")] == [3, 3]
    assert {e["topic"] for e in config["kalshi"]} == {"fed_rate", "cpi", "unemployment"} == {e["topic"] for e in config["polymarket"]}


def test_collect_once_and_exit_code(tmp_path, monkeypatch):
    config_path = tmp_path / "markets.json"
    config_path.write_text(json.dumps(CONFIG))
    record, path = collect_once(config_path, tmp_path / "runs", session=healthy_session())
    assert record["status"] == "ok" and path.exists()
    monkeypatch.setattr(requests, "Session", lambda: FakeSession({KALSHI_MARKETS_URL: FakeResponse(429, {}), GAMMA_MARKETS_URL: FakeResponse(429, {})}))
    assert main(["--config", str(config_path), "--out", str(tmp_path / "runs2")]) == 1
    monkeypatch.setattr(requests, "Session", healthy_session)
    assert main(["--config", str(config_path), "--out", str(tmp_path / "runs3")]) == 0
    assert probe.RUNS_DIR.name == "runs" and os.path.isdir(tmp_path / "runs3")
