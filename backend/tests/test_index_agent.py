"""Index agent (1016-1617), data side: candidate pool, chain detection, harmonization rules, blueprint validation. No network."""

from datetime import date, datetime, time, timedelta, timezone

import pytest

from backend.app.agent.blueprint import calculation_text, pinned_keys, resolve
from backend.app.agent.chaining import build_chains, template
from backend.app.agent.pool import Candidate, gather
from backend.app.eui.models import MarketSnapshot, SnapshotRun
from backend.tests.eui_helpers import market, memory_session

D = date(2026, 1, 1)


def cand(ext, title, first=None, last=None, end=None, source="polymarket", days=None, **kw) -> Candidate:
    days = days if days is not None else ((last - first).days + 1 if first and last else 0)
    return Candidate(source, ext, title, first, last, days, 1000.0, end, **kw)


FED = [cand("jan", "Fed cut in January 2026?", D, D + timedelta(days=30), D + timedelta(days=30)),
       cand("mar", "Fed cut in March 2026?", D + timedelta(days=31), D + timedelta(days=89), D + timedelta(days=89)),
       cand("may", "Fed cut in May 2026?", D + timedelta(days=100), D + timedelta(days=150), D + timedelta(days=150), source="kalshi")]
OTHER = [cand("cpi3", "CPI above 3% in March 2026?", D, D + timedelta(days=9), D + timedelta(days=9)),
         cand("cpi4", "CPI above 4% in March 2026?", D, D + timedelta(days=9), D + timedelta(days=9)),
         cand("plain", "Will a recession happen?", D, D + timedelta(days=19))]
POOL = FED + OTHER


def test_template_removes_only_the_period():
    assert template("Fed cut in January 2026?") == template("Fed cut in May 2026?") == template("fed cut in dec 10, 2025?")
    assert template("CPI above 3% in March 2026?") != template("CPI above 4% in March 2026?")  # thresholds never chain
    assert template("Will a recession happen?") is None
    assert template("GDP growth in Q1 2026?") == template("GDP growth in Q3 2025?")
    assert template("Close on 2026-01-05?") == template("Close on 2026-02-05?")


def test_chain_orders_links_by_end_date_and_reports_gaps():
    chains = build_chains(POOL)
    assert len(chains) == 1  # CPI thresholds and the plain market do not chain
    fed = chains[0].payload()
    assert [l["external_id"] for l in fed["links"]] == ["jan", "mar", "may"]
    assert fed["links"][0]["roll_on"] == "2026-01-31" and fed["sources"] == ["kalshi", "polymarket"]
    assert fed["first_date"] == "2026-01-01" and fed["last_date"] == "2026-05-31"
    assert fed["gaps"] == [{"from": "2026-04-01", "to": "2026-04-10", "days": 10}]
    assert fed["history_days"] == 151 - 10
    assert fed["label"] == "fed cut in …?" and fed["rolls"] == 2


def test_deadline_ladder_has_no_roll_yet():
    ladder = [cand(m, f"Deal announced by {m} 30, 2027?", D, D + timedelta(days=1), date(2027, n, 30)) for n, m in ((3, "March"), (6, "June"))]
    chains = build_chains(ladder + FED)
    assert [c.rolls for c in chains] == [2, 0]  # chains with real roll-overs first
    bp = resolve({"components": [{"ref": chains[1].key, "weight": 1}]}, chains, ladder)
    assert "Laufzeitstruktur" in [r["topic"] for r in bp["harmonization"]]


def test_same_period_twice_is_no_chain():
    twins = [cand("a", "Fed cut in March 2026?", D, D, D), cand("b", "Fed cut in March 2026?", D, D, D, source="kalshi")]
    assert build_chains(twins) == []


def test_resolve_validates_normalizes_and_harmonizes():
    chains = build_chains(POOL)
    raw = {"name": "Zinsangst", "thesis": "Fed-Lockerung", "search_terms": ["fed", "FED", "cut"],
           "components": [{"ref": chains[0].key, "direction": "up", "weight": 3, "reason": "lange Historie"},
                          {"ref": "polymarket:plain", "direction": "down", "weight": 1, "reason": "Gegenposition"},
                          {"ref": "polymarket:ghost", "direction": "up", "weight": 5, "reason": "erfunden"},
                          {"ref": chains[0].key, "direction": "up", "weight": 9, "reason": "doppelt"}],
           "harmonization": [{"topic": "Schwellen", "rule": "nur 25-bp-Schritte"}, {"topic": "Preisskala", "rule": "überschrieben?"}],
           "caveats": ["wenig Kalshi-Historie"]}
    bp = resolve(raw, chains, POOL)
    assert [c["ref"] for c in bp["components"]] == [chains[0].key, "polymarket:plain"]
    assert [c["weight"] for c in bp["components"]] == [0.75, 0.25]
    assert bp["search_terms"] == ["fed", "cut"]
    chain = bp["components"][0]
    assert (chain["kind"], chain["link_count"], len(chain["markets"]), chain["history_days"]) == ("chain", 3, 3, 141)
    topics = [r["topic"] for r in bp["harmonization"]]
    for topic in ("Preisskala", "Richtung", "Verkettung", "Restlaufzeit", "Lücken", "Quellen", "Gewichtung", "Schwellen"):
        assert topic in topics
    assert "Startdatum" not in topics and topics.count("Preisskala") == 1 and "überschrieben?" not in str(bp["harmonization"])
    assert bp["history"] == {"start": "2026-01-01", "common_start": "2026-01-01", "end": "2026-05-31", "chained": 1, "without_history": 0}
    assert pinned_keys(bp)[:3] == ["polymarket:jan", "polymarket:mar", "kalshi:may"]
    text = calculation_text(bp)
    assert "Kette, 3 Glieder" in text and "Richtung fallend" in text and "Verkettung:" in text


def test_resolve_equal_weights_and_event_rule():
    pool = [cand("a", "Candidate A wins?", event_key="ev"), cand("b", "Candidate B wins?", event_key="ev")]
    bp = resolve({"components": [{"ref": "polymarket:a", "weight": 0}, {"ref": "polymarket:b", "weight": "x"}]}, [], pool)
    assert [c["weight"] for c in bp["components"]] == [0.5, 0.5]
    assert "Mehrere Outcomes" in [r["topic"] for r in bp["harmonization"]]
    assert bp["history"]["without_history"] == 2
    assert resolve(None, [], [])["components"] == []


def add(session, day, ext, title, source="polymarket"):
    run = session.query(SnapshotRun).filter_by(snapshot_date=day).first() or SnapshotRun(snapshot_date=day, status="ok")
    session.add(run)
    session.flush()
    session.add(MarketSnapshot(run_id=run.id, snapshot_date=day, source=source, external_id=ext, title=title, tags=[], price=0.4,
                               volume_24h_usd=2000.0, end_date=datetime.combine(day, time(), tzinfo=timezone.utc),
                               fetched_at=datetime.combine(day, time(8), tzinfo=timezone.utc)))


@pytest.fixture()
def session():
    s = memory_session()
    for i in range(5):
        add(s, D + timedelta(days=i), "old", "Fed cut in January 2026?")
    add(s, D, "unrelated", "Oil above 100?")
    s.commit()
    return s


def test_gather_merges_history_with_live_state(session):
    live = [market(external_id="old", title="Fed cut in January 2026?", event_key="ev-fed", price=0.7),
            market(external_id="new", title="Fed cut in March 2026?", tags=["Fed"], end_date=datetime(2026, 3, 31, tzinfo=timezone.utc)),
            market(external_id="pin", title="Something else entirely")]
    pool = {c.key: c for c in gather(session, live, ["fed"], pinned=["polymarket:pin", "polymarket:unrelated"])}
    assert set(pool) == {"polymarket:old", "polymarket:new", "polymarket:pin", "polymarket:unrelated"}
    old = pool["polymarket:old"]
    assert (old.days, old.first_date, old.live, old.price, old.event_key) == (5, D, True, 0.7, "ev-fed")
    assert pool["polymarket:new"].days == 0 and pool["polymarket:new"].end_date == date(2026, 3, 31)
    assert len(build_chains(list(pool.values()))) == 1
    assert gather(session, [], []) == []
