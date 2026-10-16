"""Index Manufacture catalog: entries with liquidity metrics, full-text search, facets, sorting, shortlist."""

from datetime import datetime, timezone

import pytest

from backend.app.manufacture.catalog import build_catalog, by_key, facets, relevance, search, shortlist, tokenize
from backend.tests.eui_helpers import market

FED = market(external_id="fed", title="Will the Fed cut rates in December?", tags=["Economics", "Fed"],
             description="Resolves Yes if the FOMC lowers the target range.", liquidity=50_000.0, volume_24h=8_000.0)
REC = market(source="kalshi", external_id="rec", title="US recession declared in 2026?", tags=["Economics"],
             description="Based on NBER announcement.", liquidity=20_000.0, volume_24h=1_000.0, best_bid=0.2, best_ask=0.3)
NBA = market(external_id="nba", title="Lakers win the NBA title?", tags=["Sports", "NBA"], description=None,
             liquidity=90_000.0, volume_24h=40_000.0, end_date=datetime(2027, 1, 1, tzinfo=timezone.utc))
OIL = market(source="kalshi", external_id="oil", title="Brent above $90 by March?", tags=[], category="Commodities",
             description="Oil price threshold market", liquidity=12_000.0, volume_24h=None, best_bid=None, best_ask=None)
ALL = [FED, REC, NBA, OIL]


def test_build_catalog_derives_metrics_and_deduplicates():
    entries = build_catalog(ALL + [FED])

    assert [e.key for e in entries] == ["polymarket:fed", "kalshi:rec", "polymarket:nba", "kalshi:oil"]
    fed = by_key(entries)["polymarket:fed"]
    assert fed.spread == pytest.approx(0.02) and fed.liquidity_score == pytest.approx(58_000.0)
    rec = by_key(entries)["kalshi:rec"]
    assert rec.spread == pytest.approx(0.1) and rec.liquidity_score == pytest.approx(21_000.0 * 0.05 / 0.1)
    oil = by_key(entries)["kalshi:oil"]
    assert oil.tags == ("Commodities",) and oil.spread is None and oil.volume_24h == 0.0
    assert fed.description.startswith("Resolves Yes")


def test_tokenize_drops_stopwords_and_short_words():
    assert tokenize("Ich will einen Index für die Fed und US-Rezession") == ["fed", "rezession"]
    assert tokenize("Fed-Zinssenkungen, S&P 500") == ["fed", "zinssenkungen", "s&p", "500"]
    assert tokenize("") == []


def test_search_matches_title_description_and_tags_ranked_by_relevance():
    entries = build_catalog(ALL)

    fomc = search(entries, q="FOMC")
    assert [i["id"] for i in fomc["items"]] == ["polymarket:fed"] and fomc["sort"] == "relevance"
    assert fomc["items"][0]["relevance"] == pytest.approx(1.0)
    econ = search(entries, q="economics")
    assert {i["id"] for i in econ["items"]} == {"polymarket:fed", "kalshi:rec"}
    fed = search(entries, q="fed")
    assert fed["items"][0]["id"] == "polymarket:fed" and fed["items"][0]["relevance"] == pytest.approx(5.0)
    assert search(entries, q="zzz-nothing")["total"] == 0


def test_search_filters_source_tags_liquidity_and_reports_facets():
    entries = build_catalog(ALL)

    kalshi = search(entries, source="kalshi")
    assert {i["id"] for i in kalshi["items"]} == {"kalshi:rec", "kalshi:oil"}
    assert kalshi["facets"]["sources"] == [{"source": "kalshi", "count": 2}]
    tagged = search(entries, tags=["economics"])
    assert tagged["total"] == 2
    liquid = search(entries, min_liquidity=30_000)
    assert [i["id"] for i in liquid["items"]] == ["polymarket:nba", "polymarket:fed"]
    assert facets(entries)["tags"][0] == {"tag": "Economics", "count": 2}


def test_search_sorting_and_pagination():
    entries = build_catalog(ALL)

    by_liq = search(entries, sort="liquidity")
    assert [i["id"] for i in by_liq["items"]] == ["polymarket:nba", "polymarket:fed", "kalshi:rec", "kalshi:oil"]
    by_title = search(entries, sort="title")
    assert [i["title"][:5] for i in by_title["items"]] == ["Brent", "Laker", "US re", "Will "]
    asc = search(entries, sort="liquidity", descending=False)
    assert asc["items"][0]["id"] == "kalshi:oil"
    page = search(entries, sort="liquidity", limit=2, offset=2)
    assert page["total"] == 4 and [i["id"] for i in page["items"]] == ["kalshi:rec", "kalshi:oil"]
    assert search(entries, sort="bogus")["sort"] == "liquidity"


def test_top_100_preset_returns_the_most_liquid_markets_of_both_sources():
    """The "Top 100 liquideste" button asks for sort=liquidity, limit=100 without filters."""
    entries = build_catalog([market(source="kalshi" if i % 2 else "polymarket", external_id=f"m{i}", liquidity=10_000.0 + i) for i in range(150)])

    top = search(entries, sort="liquidity", limit=100)

    assert top["total"] == 150 and len(top["items"]) == 100 and top["sort"] == "liquidity"
    liquidity = [i["liquidity"] for i in top["items"]]
    assert liquidity == sorted(liquidity, reverse=True) and liquidity[0] == 10_149.0 and liquidity[-1] == 10_050.0
    assert {i["source"] for i in top["items"]} == {"kalshi", "polymarket"}
    assert top["facets"]["sources"] == [{"source": "kalshi", "count": 75}, {"source": "polymarket", "count": 75}]


def test_shortlist_puts_pinned_then_matches_then_most_liquid():
    entries = build_catalog(ALL)

    keys = [e.key for e in shortlist(entries, "recession risk", limit=3, pinned=["kalshi:oil", "unknown:x"])]
    assert keys == ["kalshi:oil", "kalshi:rec", "polymarket:nba"]
    assert relevance(by_key(entries)["kalshi:rec"], tokenize("recession")) == pytest.approx(3.0)
    assert len(shortlist(entries, "", limit=10)) == 4


def test_search_filters_by_inclusive_price_corridor_and_keeps_unknown_prices():
    priced = build_catalog([market(external_id="lo", price=0.02), market(external_id="edge", price=0.03),
                            market(external_id="mid", price=0.5), market(external_id="hi", price=0.98),
                            market(external_id="none", price=None)])

    keys = {i["external_id"] for i in search(priced, min_price=0.03, max_price=0.97)["items"]}
    assert keys == {"edge", "mid", "none"}
    assert search(priced)["total"] == 5  # no corridor given -> no restriction


def test_tag_facets_list_only_tags_of_markets_matching_the_other_filters():
    entries = build_catalog(ALL)

    kalshi = search(entries, source="kalshi")["facets"]["tags"]
    assert kalshi == [{"tag": "Commodities", "count": 1}, {"tag": "Economics", "count": 1}]
    liquid = search(entries, min_liquidity=30_000)["facets"]["tags"]
    assert {t["tag"] for t in liquid} == {"Economics", "Fed", "Sports", "NBA"}
    assert search(entries, q="brent")["facets"]["tags"] == [{"tag": "Commodities", "count": 1}]
    assert search(entries, q="zzz-nothing")["facets"]["tags"] == []


def test_tag_facets_ignore_the_tag_filter_and_keep_the_active_tag():
    entries = build_catalog(ALL)

    picked = search(entries, tags=["Sports"])
    assert picked["total"] == 1
    assert {t["tag"] for t in picked["facets"]["tags"]} == {"Economics", "Fed", "Sports", "NBA", "Commodities"}
    orphan = search(entries, source="kalshi", tags=["nba"])["facets"]["tags"]
    assert orphan[-1] == {"tag": "nba", "count": 0}
    assert facets(entries, keep_tags=["Fed"])["tags"].count({"tag": "Fed", "count": 1}) == 1


def test_tag_facets_keep_active_tag_beyond_the_facet_limit(monkeypatch):
    import backend.app.manufacture.catalog as catalog

    monkeypatch.setattr(catalog, "TAG_FACET_LIMIT", 1)
    shown = facets(build_catalog(ALL), keep_tags=["NBA"])["tags"]
    assert shown == [{"tag": "Economics", "count": 2}, {"tag": "NBA", "count": 1}]
