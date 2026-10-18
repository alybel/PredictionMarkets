"""Topic tree: topic -> subtopic -> event (with its outcome markets) or standalone market.

Topics come from the topic list of the population (``tag_counts``: tags ranked by frequency). A node's
topic is its most frequent tag, its subtopic the next one (else ``GENERAL``); nodes without tags land
under ``NO_TOPIC`` (last). Source meta tags (``is_meta_tag``: "Recurring", "Hide From New", "Earn 3.25%")
are no topics. Outcome markets of one source event (``group_markets``) form one event node
placed by the union of their tags. Built from the cached live population, so no stored data is needed.
"""

import logging

from backend.app.engine import tag_counts
from backend.app.events.service import group_markets

logger = logging.getLogger(__name__)

NO_TOPIC = "Ohne Thema"
GENERAL = "Allgemein"
META_TAGS = {"recurring", "hide from new", "featured", "daily", "weekly", "monthly", "new"}


def is_meta_tag(tag: str) -> bool:
    """Listing/promotion labels of the sources that say nothing about the subject."""
    t = tag.strip().lower()
    return t in META_TAGS or t.startswith("earn ")


def _market(m) -> dict:
    return {"source": m.source, "external_id": m.external_id, "title": m.title, "outcome_label": m.outcome_label,
            "price": m.price, "volume_24h": m.volume_24h, "liquidity": m.liquidity,
            "end_date": m.end_date.isoformat() if m.end_date else None, "url": m.url, "tags": list(m.tags or [])}


def _leaves(markets) -> list[tuple[list, dict]]:
    """(member markets, node payload) for every event and every standalone market."""
    groups, standalone = group_markets(markets)
    leaves = []
    for (source, key), outcomes in groups.items():
        outcomes = sorted(outcomes, key=lambda m: (m.price is None, -(m.price or 0.0)))
        node = {"kind": "event", "id": f"{source}:{key}", "source": source, "title": outcomes[0].event_title or outcomes[0].title,
                "outcome_count": len(outcomes), "volume_24h": sum(m.volume_24h or 0.0 for m in outcomes),
                "outcomes": [_market(m) for m in outcomes]}
        leaves.append((outcomes, node))
    leaves += [([m], {"kind": "market", "id": f"{m.source}:{m.external_id}", **_market(m)}) for m in standalone]
    return leaves


def _placed(markets) -> tuple[list[str], list[tuple[tuple[str, str], dict]]]:
    """Topic list (most frequent tag first) and every node with its (topic, subtopic) path."""
    topic_list = [e["tag"] for e in tag_counts(markets) if not is_meta_tag(e["tag"])]
    rank = {t.strip().lower(): i for i, t in enumerate(topic_list)}
    placed = []
    for members, node in _leaves(markets):
        tags = sorted({t.strip().lower() for m in members for t in (m.tags or []) if isinstance(t, str) and t.strip().lower() in rank},
                      key=rank.__getitem__)
        path = (topic_list[rank[tags[0]]], topic_list[rank[tags[1]]] if len(tags) > 1 else GENERAL) if tags else (NO_TOPIC, GENERAL)
        placed.append((path, node))
    return topic_list, placed


def build_tree(markets) -> list[dict]:
    """Topics in topic-list order with their subtopics (largest first, ``Allgemein`` last) and node counts."""
    topic_list, placed = _placed(markets)
    topics: dict[str, dict[str, dict]] = {}
    for (topic, sub), node in placed:
        entry = topics.setdefault(topic, {}).setdefault(sub, {"name": sub, "events": 0, "markets": 0})
        entry["events" if node["kind"] == "event" else "markets"] += 1
    order = {t: i for i, t in enumerate(topic_list)}
    tree = []
    for topic in sorted(topics, key=lambda t: order.get(t, len(order))):
        subs = sorted(topics[topic].values(), key=lambda s: (s["name"] == GENERAL, -(s["events"] + s["markets"]), s["name"].lower()))
        tree.append({"name": topic, "events": sum(s["events"] for s in subs), "markets": sum(s["markets"] for s in subs), "subtopics": subs})
    logger.info("topic tree built: %d topics from %d markets", len(tree), len(markets))
    return tree


def subtopic_nodes(markets, topic: str, subtopic: str) -> list[dict]:
    """Event and market nodes of one subtopic, highest 24h volume first."""
    nodes = [node for path, node in _placed(markets)[1] if path == (topic, subtopic)]
    return sorted(nodes, key=lambda n: (-(n["volume_24h"] or 0.0), n["title"].lower()))
