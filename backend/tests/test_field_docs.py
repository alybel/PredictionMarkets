"""Field explanations: liquidity basis per source, /fields endpoint and catalog payload."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.app.api import app
from backend.app.clients import KalshiClient, PolymarketClient
from backend.app.field_docs import FIELD_DOCS, LIQUIDITY_BASES
from backend.app.manufacture.catalog import build_catalog, entry_payload
from backend.tests.eui_helpers import market


def test_polymarket_liquidity_is_24h_volume_and_depth_is_kept():
    m = PolymarketClient()._normalize({"id": "1", "question": "Q?", "liquidityNum": 20000, "volume24hr": 12000})
    assert m.liquidity == m.volume_24h == pytest.approx(12000.0)
    assert m.liquidity_basis == "volume_24h"
    assert m.depth == pytest.approx(20000.0)
    assert PolymarketClient()._normalize({"id": "1", "question": "Q?", "liquidityNum": 20000}).liquidity_basis is None


@pytest.mark.parametrize("item, depth", [
    ({"liquidity_dollars": "25000"}, 25000.0),
    ({"liquidity": 2500000}, 25000.0),  # legacy cents
    ({"liquidity_dollars": "0", "open_interest_fp": "30000"}, 30000.0),
    ({"open_interest_fp": "12000"}, 12000.0),
    ({}, None),
])
def test_kalshi_liquidity_is_24h_volume_whatever_the_depth(item, depth):
    m = KalshiClient()._normalize({"ticker": "T", "title": "Q?", "last_price_dollars": "0.4", "volume_24h_fp": "1000", **item})
    assert m.liquidity == pytest.approx(400.0)  # 1000 contracts x 0.40 USD
    assert m.liquidity_basis == "volume_24h"
    assert m.depth == (pytest.approx(depth) if depth is not None else None)
    assert KalshiClient()._normalize({"ticker": "T", "title": "Q?", **item}).liquidity is None  # no 24h data


def test_every_basis_and_field_is_documented_for_both_sources():
    assert set(LIQUIDITY_BASES) == {"volume_24h"}
    keys = {d["key"] for d in FIELD_DOCS}
    assert {"price", "liquidity", "volume", "volume_24h", "spread", "liquidity_score", "min_liquidity"} <= keys
    for doc in FIELD_DOCS:
        assert all(doc[k].strip() for k in ("label", "unit", "what", "polymarket", "kalshi"))
    liquidity = next(d for d in FIELD_DOCS if d["key"] == "liquidity")
    assert "24" in liquidity["what"] and "volume24hr" in liquidity["polymarket"] and "24 h" in liquidity["kalshi"]
    assert liquidity["comparability"]


def test_fields_endpoint_serves_docs_and_bases():
    res = TestClient(app).get("/fields")
    assert res.status_code == 200
    body = res.json()
    assert [d["key"] for d in body["fields"]] == [d["key"] for d in FIELD_DOCS]
    assert body["liquidity_bases"]["volume_24h"]["label"] == "24h-Umsatz"


def test_catalog_payload_carries_liquidity_basis():
    entries = build_catalog([market(source="kalshi", external_id="K", liquidity_basis="volume_24h"), market(external_id="P")])
    payloads = {p["id"]: p for p in map(entry_payload, entries)}
    assert payloads["kalshi:K"]["liquidity_basis"] == "volume_24h"
    assert payloads["polymarket:P"]["liquidity_basis"] is None


def test_frontend_wires_field_docs():
    static = Path(__file__).resolve().parents[1] / "app" / "static"
    html = (static / "index.html").read_text()
    assert '<script src="/static/fields.js"></script>' in html
    assert html.count("data-field-docs") == 1 and 'data-doc="liquidity"' in html
    assert "liquidityCell(m)" in (static / "indices.js").read_text()


def test_catalog_liquidity_is_24h_volume_while_score_keeps_depth():
    (entry,) = build_catalog([market(liquidity=5_000.0, volume_24h=5_000.0, depth=30_000.0, best_bid=0.49, best_ask=0.51)])
    assert entry.liquidity == 5_000.0 and entry.volume_24h == 5_000.0
    assert entry.liquidity_score == pytest.approx(35_000.0)  # depth + 24h turnover, unchanged formula
