"""Market catalog of the Index Manufacture.

Turns the cached market population (both sources, above the global
liquidity floor) into searchable entries with liquidity metrics: liquidity
(= 24h turnover), total turnover, spread and the liquidity score of the
uncertainty index (order-book depth + 24h turnover). Full-text search covers title, description and tags;
``shortlist`` ranks the population against a free-text intent so the AI
assistant only sees the most relevant candidates.
"""

import re
from dataclasses import dataclass
from datetime import datetime

from backend.app.eui.harmonize import liquidity_score, spread_of

STOPWORDS = {
    "the", "and", "for", "with", "will", "that", "this", "from", "into", "about", "index", "markets", "market",
    "der", "die", "das", "und", "für", "mit", "über", "einen", "eine", "ein", "auf", "von", "zum", "zur", "ich",
    "möchte", "will", "brauche", "einem", "einer", "nicht", "oder", "wie", "was", "den", "dem", "des", "im", "am",
}
SORT_KEYS = ("liquidity", "volume", "volume_24h", "liquidity_score", "price", "spread", "end_date", "title", "relevance")
OPTIONAL_SORT_KEYS = ("price", "spread", "end_date")  # may be unknown: such markets always come last
MAX_LIMIT = 500
TAG_FACET_LIMIT = 60


@dataclass(frozen=True)
class CatalogEntry:
    source: str
    external_id: str
    title: str
    description: str | None
    tags: tuple[str, ...]
    price: float | None
    liquidity: float
    volume: float
    volume_24h: float
    spread: float | None
    liquidity_score: float
    end_date: datetime | None
    url: str | None
    liquidity_basis: str | None = None

    @property
    def key(self) -> str:
        return f"{self.source}:{self.external_id}"

    @property
    def search_text(self) -> str:
        return " ".join([self.title, self.description or "", " ".join(self.tags)]).lower()


def build_catalog(markets) -> list[CatalogEntry]:
    """Catalog entries from normalized markets; duplicates (same source + id) keep the first."""
    seen: set[str] = set()
    entries: list[CatalogEntry] = []
    for m in markets:
        key = f"{m.source}:{m.external_id}"
        if key in seen:
            continue
        seen.add(key)
        spread = spread_of(getattr(m, "best_bid", None), getattr(m, "best_ask", None))
        depth = getattr(m, "depth", None) or 0.0
        volume_24h = getattr(m, "volume_24h", None) or 0.0
        tags = tuple(t for t in (getattr(m, "tags", None) or []) if isinstance(t, str) and t.strip())
        if not tags and getattr(m, "category", None):
            tags = (m.category,)
        entries.append(CatalogEntry(
            source=m.source, external_id=m.external_id, title=m.title, description=getattr(m, "description", None),
            tags=tags, price=m.price, liquidity=m.liquidity or 0.0, volume=m.volume or 0.0, volume_24h=volume_24h,
            spread=spread, liquidity_score=liquidity_score(depth, volume_24h, spread),
            end_date=getattr(m, "end_date", None), url=getattr(m, "url", None),
            liquidity_basis=getattr(m, "liquidity_basis", None),
        ))
    return entries


def tokenize(text: str) -> list[str]:
    """Distinct lower-cased search terms of at least three characters, stopwords removed.

    Hyphenated compounds are split ("Fed-Zinssenkung" -> "fed", "zinssenkung") so each part can match.
    """
    terms: list[str] = []
    for word in re.findall(r"[\wäöüß&$%.]+", (text or "").lower()):
        word = word.strip(".")
        if len(word) >= 3 and word not in STOPWORDS and word not in terms:
            terms.append(word)
    return terms


def relevance(entry: CatalogEntry, terms: list[str]) -> float:
    """Term hits weighted by field: title 3, tags 2, description 1 (0 if no term matches)."""
    if not terms:
        return 0.0
    title, tags, description = entry.title.lower(), " ".join(entry.tags).lower(), (entry.description or "").lower()
    score = 0.0
    for term in terms:
        score += 3.0 * (term in title) + 2.0 * (term in tags) + 1.0 * (term in description)
    return score


def facets(entries: list[CatalogEntry], keep_tags=()) -> dict:
    """Source and tag counts (tags grouped case-insensitively, most frequent first).

    Tags in ``keep_tags`` (the active selection) are always listed, with count 0 if no entry carries them.
    """
    sources: dict[str, int] = {}
    tags: dict[str, dict] = {}
    for e in entries:
        sources[e.source] = sources.get(e.source, 0) + 1
        for tag in {t.lower(): t for t in e.tags}.values():
            slot = tags.setdefault(tag.lower(), {"tag": tag, "count": 0})
            slot["count"] += 1
    ranked = sorted(tags.values(), key=lambda t: (-t["count"], t["tag"].lower()))
    shown = ranked[:TAG_FACET_LIMIT]
    listed = {t["tag"].lower() for t in shown}
    for tag in keep_tags:
        if tag and tag.lower() not in listed:
            listed.add(tag.lower())
            shown.append(tags.get(tag.lower(), {"tag": tag, "count": 0}))
    return {
        "sources": [{"source": s, "count": n} for s, n in sorted(sources.items())],
        "tags": shown,
    }


def search(entries: list[CatalogEntry], q: str = "", source: str = "", tags=(), min_liquidity: float = 0.0,
           sort: str = "", descending: bool = True, limit: int = 100, offset: int = 0,
           min_price: float = 0.0, max_price: float = 1.0) -> dict:
    """Filtered, sorted page of the catalog plus total count and facets of the filtered set.

    Tag facets ignore the tag filter itself, so they list exactly the tags of markets matching all
    other criteria (and the user can switch tags); source facets cover the fully filtered set.

    An empty or unknown ``sort`` means relevance when a query is given, else liquidity. ``title`` sorts
    A–Z when ``descending`` (the default); markets without price, spread or end date sort last.
    The price corridor is inclusive; markets without a known price pass (as in the index rule).
    """
    terms = tokenize(q)
    wanted = {t.lower() for t in tags if t}
    rows: list[tuple[float, CatalogEntry]] = []
    tag_pool: list[CatalogEntry] = []
    for e in entries:
        if source and e.source != source:
            continue
        if e.liquidity < min_liquidity:
            continue
        if e.price is not None and not min_price <= e.price <= max_price:
            continue
        score = relevance(e, terms)
        if terms and score <= 0:
            continue
        tag_pool.append(e)
        if wanted and not (wanted & {t.lower() for t in e.tags}):
            continue
        rows.append((score, e))
    sort = sort if sort in SORT_KEYS else ("relevance" if terms else "liquidity")
    known = [r for r in rows if sort not in OPTIONAL_SORT_KEYS or getattr(r[1], sort) is not None]
    unknown = [r for r in rows if sort in OPTIONAL_SORT_KEYS and getattr(r[1], sort) is None]
    known.sort(key=lambda r: _sort_value(r, sort), reverse=descending if sort != "title" else not descending)
    rows = known + unknown
    limit = max(1, min(limit, MAX_LIMIT))
    page = rows[offset : offset + limit]
    return {
        "total": len(rows), "offset": offset, "limit": limit, "sort": sort, "descending": descending, "terms": terms,
        "facets": {"sources": facets([e for _, e in rows])["sources"],
                   "tags": facets(tag_pool, keep_tags=[t for t in tags if t])["tags"]},
        "items": [entry_payload(e, score if terms else None) for score, e in page],
    }


def _sort_value(row: tuple[float, CatalogEntry], sort: str):
    score, e = row
    if sort == "relevance":
        return (score, e.liquidity)
    if sort == "title":
        return e.title.lower()
    if sort == "end_date":
        return e.end_date.timestamp()
    return getattr(e, sort)


def entry_payload(e: CatalogEntry, score: float | None = None) -> dict:
    payload = {
        "id": e.key, "source": e.source, "external_id": e.external_id, "title": e.title, "description": e.description,
        "tags": list(e.tags), "price": e.price, "liquidity": e.liquidity, "volume": e.volume, "volume_24h": e.volume_24h,
        "spread": e.spread, "liquidity_score": e.liquidity_score, "liquidity_basis": e.liquidity_basis,
        "end_date": e.end_date.isoformat() if e.end_date else None, "url": e.url,
    }
    if score is not None:
        payload["relevance"] = score
    return payload


def by_key(entries: list[CatalogEntry]) -> dict[str, CatalogEntry]:
    return {e.key: e for e in entries}


def shortlist(entries: list[CatalogEntry], intent: str, limit: int = 120, pinned=()) -> list[CatalogEntry]:
    """Candidates for the assistant: pinned keys first, then the best term matches, then the most liquid markets."""
    index = by_key(entries)
    chosen: list[CatalogEntry] = [index[k] for k in pinned if k in index]
    taken = {e.key for e in chosen}
    terms = tokenize(intent)
    ranked = sorted(((relevance(e, terms), e) for e in entries if e.key not in taken), key=lambda r: (-r[0], -r[1].liquidity))
    for score, e in ranked:
        if len(chosen) >= limit:
            break
        if score > 0:
            chosen.append(e)
            taken.add(e.key)
    for e in sorted((e for e in entries if e.key not in taken), key=lambda e: -e.liquidity):
        if len(chosen) >= limit:
            break
        chosen.append(e)
    return chosen
