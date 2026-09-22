"""Index parameters from the dashboard: preview, save, create, delete — no network."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from backend.app.api import app, get_markets, get_session
from backend.app.clients.base import NormalizedMarket
from backend.app.db import Base, make_session_factory
from backend.app.engine import get_rule, tag_counts, upsert_rule


def make_market(external_id, title, tags, price=0.5, liquidity=20_000.0):
    return NormalizedMarket(
        source="polymarket", external_id=external_id, title=title, category=tags[0] if tags else None,
        tags=tags, price=price, volume=100.0, liquidity=liquidity,
    )


MARKETS = [
    make_market("a", "Will the president win the election?", ["Politics", "Elections"], price=0.2),
    make_market("b", "Fed rate cut in December?", ["Economics", "Fed"], price=0.8, liquidity=60_000.0),
    make_market("c", "Recession declared this year?", ["economics"], price=0.6),
    make_market("d", "Champions League winner?", ["Sports"], price=0.4),
]


@pytest.fixture()
def client():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with make_session_factory(engine)() as session:
        upsert_rule(session, "political", categories=["Politics"])
        upsert_rule(session, "economics", categories=["Economics"], keywords=["fed"])
        app.dependency_overrides[get_session] = lambda: session
        app.dependency_overrides[get_markets] = lambda: MARKETS
        yield TestClient(app), session
    app.dependency_overrides.clear()


RULE = {"categories": ["Economics"], "keywords": [], "min_liquidity": 10_000, "min_price": 0.03, "max_price": 0.97}


# ---- preview ----------------------------------------------------------------

def test_preview_has_same_shape_as_get_and_leaves_rule_untouched(client):
    api, session = client
    stored = api.get("/indices/economics").json()

    preview = api.post("/indices/economics/preview", json=RULE).json()

    assert set(preview) == set(stored)
    assert set(preview["funnel"]) == set(stored["funnel"])
    assert [m["external_id"] for m in preview["markets"]] == ["b", "c"]  # keywords dropped: c joins
    assert preview["value"] == pytest.approx((0.8 * 60_000 + 0.6 * 20_000) / 80_000)
    assert get_rule(session, "economics").keywords == ["fed"]  # DB unchanged
    assert [m["external_id"] for m in api.get("/indices/economics").json()["markets"]] == ["b"]


def test_preview_applies_every_parameter(client):
    api, _ = client
    body = api.post(
        "/indices/whatever/preview",
        json={**RULE, "keywords": ["recession"], "min_liquidity": 15_000, "min_price": 0.5, "max_price": 0.7},
    ).json()
    assert body["index_name"] == "whatever"
    assert [m["external_id"] for m in body["markets"]] == ["c"]
    assert body["funnel"]["keywords"] == 1


@pytest.mark.parametrize(
    "override",
    [{"min_liquidity": 9_999}, {"min_price": -0.1}, {"max_price": 1.1}, {"min_price": 0.5, "max_price": 0.5}],
)
def test_preview_rejects_invalid_values(client, override):
    api, _ = client
    assert api.post("/indices/economics/preview", json={**RULE, **override}).status_code == 400


# ---- update ---------------------------------------------------------------------

def test_put_persists_rule(client):
    api, session = client
    res = api.put("/indices/economics", json={**RULE, "keywords": [" Fed ", "fed", "", "rate"]})

    assert res.status_code == 200
    assert res.json()["keywords"] == ["Fed", "rate"]  # trimmed, de-duplicated
    rule = get_rule(session, "economics")
    assert rule.categories == ["Economics"] and rule.keywords == ["Fed", "rate"]
    assert [r["keywords"] for r in api.get("/indices").json() if r["index_name"] == "economics"] == [["Fed", "rate"]]


def test_put_unknown_index_404_and_invalid_values_400(client):
    api, _ = client
    assert api.put("/indices/missing", json=RULE).status_code == 404
    assert api.put("/indices/economics", json={**RULE, "min_liquidity": 100}).status_code == 400


# ---- create / delete ------------------------------------------------------------

def test_post_creates_index_and_rejects_duplicates(client):
    api, session = client
    res = api.post("/indices", json={"index_name": " sports ", **RULE, "categories": ["Sports"]})

    assert res.status_code == 201
    assert res.json()["index_name"] == "sports"
    assert get_rule(session, "sports").categories == ["Sports"]
    assert [m["external_id"] for m in api.get("/indices/sports").json()["markets"]] == ["d"]
    assert api.post("/indices", json={"index_name": "sports", **RULE}).status_code == 409


@pytest.mark.parametrize("body", [{"index_name": "   ", **RULE}, {"index_name": "x", **RULE, "max_price": 2}])
def test_post_rejects_invalid_input(client, body):
    api, _ = client
    assert api.post("/indices", json=body).status_code == 400


def test_delete_removes_index(client):
    api, session = client
    assert api.delete("/indices/political").status_code == 204
    assert get_rule(session, "political") is None
    assert api.get("/indices/political").status_code == 404
    assert [r["index_name"] for r in api.get("/indices").json()] == ["economics"]
    assert api.delete("/indices/political").status_code == 404


# ---- tags ------------------------------------------------------------------------

def test_tags_lists_delivered_tags_with_counts(client):
    api, _ = client
    body = api.get("/tags").json()
    assert body[0] == {"tag": "Economics", "count": 2}  # "economics" grouped case-insensitively
    assert {t["tag"] for t in body} == {"Economics", "Politics", "Elections", "Fed", "Sports"}


def test_tag_counts_ignores_missing_tags():
    assert tag_counts([make_market("x", "t", []), make_market("y", "t", ["A"])]) == [{"tag": "A", "count": 1}]


# ---- dashboard -------------------------------------------------------------------

def test_dashboard_serves_parameter_panel(client):
    api, _ = client
    html = api.get("/").text
    for element_id in ("params", "keyword-chips", "tag-list", "min-liquidity", "min-price", "max-price",
                       "dirty-badge", "save-rule", "discard-rule", "new-index", "delete-index", "delete-confirm"):
        assert f'id="{element_id}"' in html
    assert "ungespeicherte Änderungen" in html
    assert "mindestens 10 000 USD" in html
    assert '/static/params.js' in html and '/static/style.css' in html


def test_dashboard_scripts_are_served_and_call_the_endpoints(client):
    api, _ = client
    js = api.get("/static/params.js")
    assert js.status_code == 200
    assert '"/preview"' in js.text and 'method: "PUT"' in js.text
    assert 'method: "DELETE"' in js.text and 'fetch("/indices", { method: "POST"' in js.text
    assert 'fetch("/tags")' in js.text
    assert api.get("/static/style.css").status_code == 200
    html = api.get("/").text
    assert '"index-loaded"' in html  # panel syncs with the selected index
