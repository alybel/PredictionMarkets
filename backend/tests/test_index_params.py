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


# ---- archive: read only (1015-2157), but deletable (1015-2231-indizes-loschen-konnen) ---

def test_archive_refuses_update_and_create(client):
    api, session = client
    assert api.put("/indices/economics", json=RULE).status_code == 409
    assert api.put("/indices/missing", json=RULE).status_code == 404
    assert api.post("/indices", json={"index_name": "sports", **RULE}).status_code == 409
    assert get_rule(session, "political") is not None and get_rule(session, "sports") is None
    assert get_rule(session, "economics").keywords == ["fed"]


# ---- tags ------------------------------------------------------------------------

def test_tags_lists_delivered_tags_with_counts(client):
    api, _ = client
    body = api.get("/tags").json()
    assert body[0] == {"tag": "Economics", "count": 2}  # "economics" grouped case-insensitively
    assert {t["tag"] for t in body} == {"Economics", "Politics", "Elections", "Fed", "Sports"}


def test_tag_counts_ignores_missing_tags():
    assert tag_counts([make_market("x", "t", []), make_market("y", "t", ["A"])]) == [{"tag": "A", "count": 1}]


# ---- dashboard -------------------------------------------------------------------

def test_dashboard_shows_the_archive_rules_read_only(client):
    api, _ = client
    html = api.get("/").text
    aside = html[html.index('<aside class="ib-side">'):html.index("</aside>")]
    assert 'id="rule-summary"' in aside and "Archiv, nur lesbar" in aside
    for element_id in ("keyword-chips", "tag-list", "min-liquidity", "min-price", "max-price", "save-rule", "delete-index"):
        assert f'id="{element_id}"' not in html
    assert api.get("/static/params.js").status_code == 404 and '/static/style.css' in html
    assert api.get("/static/style.css").status_code == 200
    assert '"index-loaded"' in api.get("/static/indices.js").text + api.get("/static/builder.js").text
