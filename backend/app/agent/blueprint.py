"""Index blueprint: the agent's starting point for an index, validated against the real candidate pool.

A component references a chain (``chain:<hash>``) or one market (``source:external_id``). Unknown references
are dropped and logged, weights are normalized to 100 %, and every component carries its real history
(first/last stored day, days, gaps), so the blueprint only ever promises data that exists.
"""

import logging
from collections import Counter

from backend.app.agent import harmonize
from backend.app.agent.chaining import Chain
from backend.app.agent.pool import Candidate

logger = logging.getLogger(__name__)

MAX_COMPONENTS = 20
MAX_TEXT = 4000
DIRECTIONS = ("up", "down")

COMPONENT_SCHEMA = {
    "type": "object",
    "properties": {"ref": {"type": "string"}, "direction": {"type": "string", "enum": list(DIRECTIONS)},
                   "weight": {"type": "number"}, "reason": {"type": "string"}},
    "required": ["ref", "direction", "weight", "reason"], "additionalProperties": False,
}
RULE_SCHEMA = {"type": "object", "properties": {"topic": {"type": "string"}, "rule": {"type": "string"}},
               "required": ["topic", "rule"], "additionalProperties": False}
_STRINGS = {"type": "array", "items": {"type": "string"}}
BLUEPRINT_SCHEMA = {
    "type": "object",
    "properties": {"name": {"type": "string"}, "thesis": {"type": "string"}, "search_terms": _STRINGS,
                   "components": {"type": "array", "items": COMPONENT_SCHEMA},
                   "harmonization": {"type": "array", "items": RULE_SCHEMA}, "caveats": _STRINGS},
    "required": ["name", "thesis", "search_terms", "components", "harmonization", "caveats"], "additionalProperties": False,
}


def _text(value, limit: int) -> str:
    return value.strip()[:limit] if isinstance(value, str) else ""


def _strings(values, limit: int = 12) -> list[str]:
    seen, out = set(), []
    for v in values if isinstance(values, list) else []:
        s = _text(v, 200)
        if s and s.lower() not in seen:
            seen.add(s.lower())
            out.append(s)
    return out[:limit]


def _market(c: Candidate) -> dict:
    return {"source": c.source, "external_id": c.external_id, "title": c.title, "event_key": c.event_key}


def _component(ref: str, chains: dict[str, Chain], pool: dict[str, Candidate]) -> dict | None:
    if ref in chains:
        ch = chains[ref]
        return {"ref": ref, "kind": "chain", "label": ch.label, "sources": ch.sources, "link_count": len(ch.links), "rolls": ch.rolls,
                "first_date": ch.payload()["first_date"], "last_date": ch.payload()["last_date"], "history_days": ch.history_days,
                "gaps": ch.gaps, "markets": [_market(l.market) for l in ch.links]}
    if ref in pool:
        c = pool[ref]
        return {"ref": ref, "kind": "market", "label": c.title, "sources": [c.source], "link_count": 1, "rolls": 0,
                "first_date": c.first_date.isoformat() if c.first_date else None,
                "last_date": c.last_date.isoformat() if c.last_date else None, "history_days": c.days, "gaps": [],
                "markets": [_market(c)]}
    return None


def _weight(value) -> float:
    try:
        return max(float(value), 0.0)
    except (TypeError, ValueError):
        return 0.0


def resolve(raw, chains: list[Chain], pool: list[Candidate]) -> dict:
    """Cleaned blueprint: known components only, weights summing to 1, real history, harmonization rules."""
    raw = raw if isinstance(raw, dict) else {}
    by_chain, by_key = {c.key: c for c in chains}, {c.key: c for c in pool}
    components, seen = [], set()
    for item in raw.get("components") or []:
        ref = item.get("ref") if isinstance(item, dict) else None
        if not isinstance(ref, str) or ref in seen:
            continue
        resolved = _component(ref, by_chain, by_key)
        if resolved is None:
            logger.warning("agent proposed unknown component %s: dropped", ref)
            continue
        seen.add(ref)
        direction = item.get("direction") if item.get("direction") in DIRECTIONS else "up"
        components.append({**resolved, "direction": direction, "weight": _weight(item.get("weight")),
                           "reason": _text(item.get("reason"), 600)})
        if len(components) >= MAX_COMPONENTS:
            break
    total = sum(c["weight"] for c in components)
    for c in components:
        c["weight"] = round(c["weight"] / total if total > 0 else 1 / len(components), 4)
    event_sizes = Counter(c.event_key for c in pool if c.event_key)
    rules = harmonize.merge(harmonize.rules(components, event_sizes), raw.get("harmonization") or [])
    return {"name": _text(raw.get("name"), 128), "thesis": _text(raw.get("thesis"), MAX_TEXT),
            "search_terms": _strings(raw.get("search_terms")), "components": components, "harmonization": rules,
            "caveats": _strings(raw.get("caveats"), 8), "history": history(components)}


def history(components: list[dict]) -> dict:
    """Earliest start, common start (all components present) and latest stored day of the blueprint."""
    starts = [c["first_date"] for c in components if c["first_date"]]
    ends = [c["last_date"] for c in components if c["last_date"]]
    return {"start": min(starts, default=None), "common_start": max(starts, default=None), "end": max(ends, default=None),
            "chained": sum(1 for c in components if c["kind"] == "chain"), "without_history": sum(1 for c in components if not c["first_date"])}


def pinned_keys(blueprint: dict | None) -> list[str]:
    """Market keys of a previous blueprint (kept in the pool so its components resolve again)."""
    return [f"{m['source']}:{m['external_id']}" for c in (blueprint or {}).get("components", []) for m in c.get("markets", [])
            if isinstance(m, dict) and m.get("source") and m.get("external_id")]


def calculation_text(bp: dict) -> str:
    """Methodology text of the metric draft created from a blueprint."""
    parts = [bp.get("thesis") or ""]
    parts += [f"- {c['label']} ({'Kette, ' + str(c['link_count']) + ' Glieder' if c['kind'] == 'chain' else 'Markt'}; "
              f"Richtung {'fallend' if c['direction'] == 'down' else 'steigend'}; Gewicht {round(c['weight'] * 100, 1)} %)"
              for c in bp.get("components", [])]
    parts += [f"{r['topic']}: {r['rule']}" for r in bp.get("harmonization", [])]
    return "\n".join(p for p in parts if p)[:MAX_TEXT]
