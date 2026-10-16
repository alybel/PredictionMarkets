"""Index Manufacture catalog: sorting by every metric, both directions, unknown values last (backend; the UI tab was removed in 1015-2232)."""

import pytest
from fastapi.testclient import TestClient

from backend.app.api import app
from backend.app.manufacture.api import get_catalog_markets
from backend.app.manufacture.catalog import build_catalog, search
from backend.tests.eui_helpers import market

MARKETS = [
    market(external_id="a", title="Alpha", price=0.40, liquidity=30_000.0, volume=5_000.0, volume_24h=900.0,
           best_bid=0.39, best_ask=0.41),
    market(source="kalshi", external_id="b", title="Beta", price=0.80, liquidity=60_000.0, volume=90_000.0,
           volume_24h=100.0, best_bid=0.70, best_ask=0.90),
    market(external_id="c", title="Gamma", price=None, liquidity=45_000.0, volume=20_000.0, volume_24h=4_000.0,
           best_bid=None, best_ask=None, end_date=None),
]


def ids(result) -> list[str]:
    return [i["external_id"] for i in result["items"]]


@pytest.mark.parametrize(("sort", "expected"), [
    ("liquidity", ["b", "c", "a"]),
    ("volume", ["b", "c", "a"]),
    ("volume_24h", ["c", "a", "b"]),
    ("price", ["b", "a", "c"]),
    ("spread", ["b", "a", "c"]),
])
def test_every_metric_sorts_both_directions(sort, expected):
    entries = build_catalog(MARKETS)

    desc = search(entries, sort=sort)
    assert ids(desc) == expected and desc["sort"] == sort and desc["descending"] is True
    known = [i for i in expected if i != "c" or sort not in ("price", "spread")]
    asc = search(entries, sort=sort, descending=False)
    assert ids(asc) == known[::-1] + [i for i in expected if i not in known]


def test_unknown_values_always_come_last():
    entries = build_catalog(MARKETS)

    for descending in (True, False):
        assert ids(search(entries, sort="price", descending=descending))[-1] == "c"
        assert ids(search(entries, sort="spread", descending=descending))[-1] == "c"
        assert ids(search(entries, sort="end_date", descending=descending))[-1] == "c"


def test_api_passes_sort_direction():
    app.dependency_overrides[get_catalog_markets] = lambda: MARKETS
    try:
        client = TestClient(app)
        data = client.get("/manufacture/catalog", params={"sort": "spread", "desc": "false", "min_price": 0.0}).json()
        assert ids(data) == ["a", "b", "c"] and data["sort"] == "spread" and data["descending"] is False
        data = client.get("/manufacture/catalog", params={"sort": "price"}).json()
        assert ids(data) == ["b", "a", "c"] and data["descending"] is True
    finally:
        app.dependency_overrides.clear()
