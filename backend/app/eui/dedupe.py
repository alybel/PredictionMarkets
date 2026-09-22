"""Merge the same question offered on both platforms into one component.

Two markets match when their end dates fall on the same day (+-1), their
numbers agree and their title keywords overlap strongly (Jaccard). Matching
is only attempted across platforms and only when it is unambiguous; in
doubt the markets stay separate. Merged components add liquidity and
turnover and average the price weighted by liquidity score.
"""

import re
from dataclasses import dataclass, field

JACCARD_THRESHOLD = 0.6
END_DATE_TOLERANCE_DAYS = 1
STOPWORDS = {
    "will", "the", "be", "by", "on", "in", "of", "a", "an", "to", "at", "for", "than", "or", "and", "is",
    "does", "do", "before", "after", "end", "this", "that", "it", "its", "there", "any", "yes", "no",
    "market", "question", "resolve", "resolves", "happen", "occur",
}
_TOKEN = re.compile(r"[a-z0-9%.]+")


@dataclass
class Component:
    """One index constituent: a single market or a merged cross-platform pair."""

    component_id: str
    title: str
    members: list  # HarmonizedMarket
    price: float
    depth_usd: float
    volume_24h_usd: float
    liquidity_score: float
    sources: list[str] = field(default_factory=list)

    @property
    def primary(self):
        """Member with the highest liquidity score (carries the polarity decision)."""
        return max(self.members, key=lambda m: m.liquidity_score)


def title_tokens(title: str) -> set[str]:
    tokens = {t.strip(".") for t in _TOKEN.findall(title.lower())}
    return {t for t in tokens if t and t not in STOPWORDS}


def numbers_in(tokens: set[str]) -> set[str]:
    return {t for t in tokens if any(ch.isdigit() for ch in t)}


def similarity(a: str, b: str) -> float:
    ta, tb = title_tokens(a), title_tokens(b)
    if not ta or not tb or numbers_in(ta) != numbers_in(tb):
        return 0.0
    return len(ta & tb) / len(ta | tb)


def _same_end_date(a, b) -> bool:
    if a.end_date is None or b.end_date is None:
        return False
    return abs((a.end_date.date() - b.end_date.date()).days) <= END_DATE_TOLERANCE_DAYS


def _weighted_price(members) -> float:
    total = sum(m.liquidity_score for m in members)
    if total > 0:
        return sum(m.price * m.liquidity_score for m in members) / total
    return sum(m.price for m in members) / len(members)


def make_component(members) -> Component:
    members = sorted(members, key=lambda m: -m.liquidity_score)
    return Component(
        component_id="+".join(f"{m.source}:{m.external_id}" for m in members),
        title=members[0].title,
        members=members,
        price=_weighted_price(members),
        depth_usd=sum(m.depth_usd for m in members),
        volume_24h_usd=sum(m.volume_24h_usd for m in members),
        liquidity_score=sum(m.liquidity_score for m in members),
        sources=[m.source for m in members],
    )


def merge_duplicates(markets, threshold: float = JACCARD_THRESHOLD) -> list[Component]:
    """Components from harmonized markets; cross-platform duplicates are merged pairwise.

    A pair merges only when each side is the other's sole candidate above the
    threshold (mutual, unambiguous match); everything else stays separate.
    """
    by_source: dict[str, list] = {}
    for m in markets:
        by_source.setdefault(m.source, []).append(m)
    sources = sorted(by_source)
    if len(sources) != 2:
        return [make_component([m]) for m in markets]
    left, right = by_source[sources[0]], by_source[sources[1]]
    candidates_of_left: dict[tuple[str, str], list] = {a.key: [] for a in left}
    candidates_of_right: dict[tuple[str, str], list] = {b.key: [] for b in right}
    for a in left:
        for b in right:
            if _same_end_date(a, b) and similarity(a.title, b.title) >= threshold:
                candidates_of_left[a.key].append(b)
                candidates_of_right[b.key].append(a)
    components: list[Component] = []
    merged_right: set[tuple[str, str]] = set()
    for a in left:
        partners = candidates_of_left[a.key]
        if len(partners) == 1 and len(candidates_of_right[partners[0].key]) == 1:
            merged_right.add(partners[0].key)
            components.append(make_component([a, partners[0]]))
        else:
            components.append(make_component([a]))
    components.extend(make_component([b]) for b in right if b.key not in merged_right)
    return components
