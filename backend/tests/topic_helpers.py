"""Test helper: a risk topic whose review list holds the given markets (directory row + assessment), no classifier."""

from datetime import datetime, timezone

from backend.app.directory.models import DirectoryMarket
from backend.app.risk.models import ACCEPTED, RiskAssessment, RiskTopic

T0 = datetime(2026, 10, 1, tzinfo=timezone.utc)


def review_list(session, rows, name: str = "Marktcrash-Risiko") -> RiskTopic:
    """``rows``: (source, external_id, title, reading, status[, end datetime]); returns the topic."""
    topic = RiskTopic(slug=name.lower()[:64], name=name, question="Wie wahrscheinlich ist ein Crash?", search_terms=[], tags=[], created_at=T0)
    session.add(topic)
    session.flush()
    for source, eid, title, reading, status, *end in rows:
        m = DirectoryMarket(source=source, external_id=eid, title=title, tags=[], status="active", first_seen_at=T0, end_date=end[0] if end else None)
        session.add(m)
        session.flush()
        session.add(RiskAssessment(topic_id=topic.id, market_id=m.id, relevant=True, rationale="", proposed_reading=reading, reading=reading,
                                   status=status, model="fake", created_at=T0))
    session.commit()
    return topic


def accept_all(session, keys, title_of=lambda eid: eid) -> RiskTopic:
    """Topic with every ``(source, external_id)`` accepted, reading "high" (p)."""
    return review_list(session, [(s, e, title_of(e), "high", ACCEPTED) for s, e in keys])
