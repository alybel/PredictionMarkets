"""Cross-platform duplicates merge into one component; in doubt markets stay separate."""

from datetime import timedelta

import pytest

from backend.app.eui.dedupe import merge_duplicates, similarity
from backend.app.eui.harmonize import harmonize
from backend.tests.eui_helpers import FUTURE, market


def h(**kw):
    return harmonize(market(**kw))


def test_same_question_on_both_platforms_is_merged_with_weighted_price():
    pm = h(source="polymarket", external_id="a", title="Will the Fed cut rates in December 2026?", price=0.60, liquidity=3000.0, volume_24h=0.0)
    ks = h(source="kalshi", external_id="b", title="Fed cuts rates in December 2026?", price=0.40, liquidity=1000.0, volume_24h=0.0)
    components = merge_duplicates([pm, ks])
    assert len(components) == 1
    c = components[0]
    assert c.component_id == "polymarket:a+kalshi:b"
    assert c.sources == ["polymarket", "kalshi"]
    assert c.price == pytest.approx(0.55)  # weighted by liquidity score 3000 vs 1000
    assert c.liquidity_score == 4000.0 and c.depth_usd == 4000.0
    assert c.primary.external_id == "a"


def test_different_numbers_or_end_dates_stay_separate():
    pm = h(source="polymarket", external_id="a", title="Unemployment above 5% in 2026?")
    ks_other_number = h(source="kalshi", external_id="b", title="Unemployment above 4% in 2026?")
    ks_other_date = h(source="kalshi", external_id="c", title="Unemployment above 5% in 2026?", end_date=FUTURE + timedelta(days=30))
    assert len(merge_duplicates([pm, ks_other_number])) == 2
    assert len(merge_duplicates([pm, ks_other_date])) == 2
    assert similarity("Will there be a recession?", "Will Bitcoin hit 100k?") < 0.6


def test_ambiguous_matches_and_same_source_never_merge():
    pm = h(source="polymarket", external_id="a", title="Fed cuts rates in December 2026?")
    ks1 = h(source="kalshi", external_id="b", title="Fed cuts rates December 2026?")
    ks2 = h(source="kalshi", external_id="c", title="Fed cut rates in December 2026")
    assert len(merge_duplicates([pm, ks1, ks2])) == 3  # two equally good candidates: keep apart
    pm2 = h(source="polymarket", external_id="d", title="Fed cuts rates in December 2026?")
    assert len(merge_duplicates([pm, pm2])) == 2  # same platform is never merged
