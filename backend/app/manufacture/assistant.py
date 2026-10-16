"""AI assistant of the Index Manufacture: proposes markets for an intent and co-writes the index description.

The assistant sees the conversation so far, the current draft and a
shortlist of candidate markets (see ``catalog.shortlist``) and answers
with a reply for the chat plus the complete updated draft (structured
JSON). Market picks are validated against the shortlist: unknown ids are
dropped and logged, so the draft only ever references real markets.
Without an API key a keyword fallback proposes the best term matches so
the workflow stays usable (marked in ``model``).
"""

import json
import logging
import os
from dataclasses import dataclass
from typing import Protocol

from backend.app.manufacture.catalog import CatalogEntry, entry_payload, relevance, tokenize
from backend.app.models import MIN_LIQUIDITY_USD

logger = logging.getLogger(__name__)

MANUFACTURE_MODEL = "claude-opus-5"   # override with MANUFACTURE_MODEL
CANDIDATE_LIMIT = 120                 # markets shown to the model per request
MAX_DRAFT_MARKETS = 40
FALLBACK_PICKS = 15
MAX_TEXT = 4000

SYSTEM_PROMPT = """You are the Index Manufacture assistant of a prediction-market index platform (Polymarket + Kalshi).
The user describes an intent (a theme, risk or question an index should track). You help turn it into an index:
1. Propose which of the candidate markets belong in the index. Prefer liquid markets (depth, 24h turnover,
   tight spread) that clearly express the intent; give a one-sentence reason per market. Never invent markets:
   use only ids from candidate_markets. Keep the user's explicit choices unless they ask to remove them.
2. Draft and refine the index description with the user: a short name, a description (what the index measures
   and why it matters), a methodology (which questions qualify, how the price is read, liquidity weighting,
   rebalancing), keywords (title terms that qualify a market for the rule engine), tags (from the candidates'
   tags) and a minimum liquidity in USD (never below the floor given in the context).
3. Answer in the user's language, concisely. Ask one focused question when the intent is ambiguous, but still
   return your best draft. Always return the complete draft, not a diff."""

DRAFT_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "description": {"type": "string"},
        "methodology": {"type": "string"},
        "keywords": {"type": "array", "items": {"type": "string"}},
        "tags": {"type": "array", "items": {"type": "string"}},
        "min_liquidity": {"type": "number"},
        "markets": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"id": {"type": "string"}, "reason": {"type": "string"}},
                "required": ["id", "reason"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["name", "description", "methodology", "keywords", "tags", "min_liquidity", "markets"],
    "additionalProperties": False,
}
OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {"reply": {"type": "string"}, "draft": DRAFT_SCHEMA},
    "required": ["reply", "draft"],
    "additionalProperties": False,
}


class AssistantError(Exception):
    """The model could not produce a usable answer (API error, refusal, unparsable output)."""


@dataclass(frozen=True)
class AssistantResult:
    reply: str
    draft: dict
    model: str


class Assistant(Protocol):
    name: str

    def respond(self, messages: list[dict], draft: dict, candidates: list[CatalogEntry]) -> AssistantResult: ...


def empty_draft() -> dict:
    return {"name": "", "description": "", "methodology": "", "keywords": [], "tags": [],
            "min_liquidity": MIN_LIQUIDITY_USD, "markets": []}


def normalize_draft(raw, candidates: list[CatalogEntry], previous: dict | None = None) -> dict:
    """Cleaned draft: strings capped, lists deduplicated, liquidity at or above the floor, markets resolved.

    Market ids the shortlist does not contain are kept when the previous draft had them (the user's own
    picks from the catalog) and dropped otherwise.
    """
    raw = raw if isinstance(raw, dict) else {}
    previous = previous or {}
    known = {e.key: e for e in candidates}
    kept = {m.get("id"): m for m in previous.get("markets", []) if isinstance(m, dict) and m.get("id")}
    markets: list[dict] = []
    seen: set[str] = set()
    for pick in raw.get("markets") or []:
        if not isinstance(pick, dict) or not isinstance(pick.get("id"), str) or pick["id"] in seen:
            continue
        reason = str(pick.get("reason") or "")[:512]
        entry = known.get(pick["id"])
        if entry is not None:
            markets.append({**_market_ref(entry), "reason": reason})
        elif pick["id"] in kept:
            markets.append({**kept[pick["id"]], "reason": reason or kept[pick["id"]].get("reason", "")})
        else:
            logger.warning("assistant proposed unknown market %s: dropped", pick["id"])
            continue
        seen.add(pick["id"])
        if len(markets) >= MAX_DRAFT_MARKETS:
            break
    try:
        min_liquidity = float(raw.get("min_liquidity", previous.get("min_liquidity", MIN_LIQUIDITY_USD)))
    except (TypeError, ValueError):
        min_liquidity = MIN_LIQUIDITY_USD
    return {
        "name": _text(raw.get("name"), 128), "description": _text(raw.get("description"), MAX_TEXT),
        "methodology": _text(raw.get("methodology"), MAX_TEXT),
        "keywords": _clean_list(raw.get("keywords")), "tags": _clean_list(raw.get("tags")),
        "min_liquidity": max(min_liquidity, MIN_LIQUIDITY_USD), "markets": markets,
    }


def _market_ref(entry: CatalogEntry) -> dict:
    return {"id": entry.key, "source": entry.source, "external_id": entry.external_id, "title": entry.title,
            "url": entry.url, "liquidity": entry.liquidity}


def _text(value, limit: int) -> str:
    return str(value).strip()[:limit] if isinstance(value, str) else ""


def _clean_list(values) -> list[str]:
    seen: set[str] = set()
    cleaned: list[str] = []
    for value in values if isinstance(values, list) else []:
        item = str(value).strip()[:64]
        if item and item.lower() not in seen:
            seen.add(item.lower())
            cleaned.append(item)
    return cleaned


def last_user_text(messages: list[dict]) -> str:
    return next((str(m.get("content", "")) for m in reversed(messages) if m.get("role") == "user"), "")


class KeywordAssistant:
    """Term-overlap stand-in when no language model is configured (marked in ``model``)."""

    name = "keyword-fallback"

    def respond(self, messages: list[dict], draft: dict, candidates: list[CatalogEntry]) -> AssistantResult:
        intent = " ".join(str(m.get("content", "")) for m in messages if m.get("role") == "user")
        terms = tokenize(intent)
        ranked = sorted(((relevance(e, terms), e) for e in candidates), key=lambda r: (-r[0], -r[1].liquidity))
        picks = [{"id": e.key, "reason": f"Titel/Tags enthalten: {', '.join(t for t in terms if t in e.search_text)}"}
                 for score, e in ranked[:FALLBACK_PICKS] if score > 0]
        keywords = draft.get("keywords") or terms[:8]
        raw = {**draft, "name": draft.get("name") or " ".join(terms[:3]).title(), "keywords": keywords,
               "markets": [m for m in draft.get("markets", [])] + picks}
        reply = ("Kein KI-Modell konfiguriert (ANTHROPIC_API_KEY fehlt). Ich habe die Märkte nach Stichwort-Treffern "
                 f"vorgeschlagen ({len(picks)} Treffer); Beschreibung und Methodik bitte selbst formulieren.")
        return AssistantResult(reply, normalize_draft(raw, candidates, draft), self.name)


class AnthropicAssistant:
    """One Claude call per turn: transcript + context (draft, candidates) in, reply + full draft out."""

    def __init__(self, client=None, model: str = MANUFACTURE_MODEL):
        if client is None:
            import anthropic

            client = anthropic.Anthropic()
        self._client = client
        self.name = model

    def respond(self, messages: list[dict], draft: dict, candidates: list[CatalogEntry]) -> AssistantResult:
        context = {"liquidity_floor_usd": MIN_LIQUIDITY_USD, "current_draft": draft,
                   "candidate_markets": [_candidate(e) for e in candidates]}
        history = [{"role": m["role"], "content": str(m.get("content", ""))} for m in messages if m.get("role") in ("user", "assistant")]
        if not history or history[-1]["role"] != "user":
            raise AssistantError("the last message must come from the user")
        history[-1] = {"role": "user", "content": history[-1]["content"] + "\n\n<context>\n" + json.dumps(context, ensure_ascii=False) + "\n</context>"}
        try:
            response = self._client.messages.create(
                model=self.name, max_tokens=16000, system=SYSTEM_PROMPT, messages=history,
                output_config={"format": {"type": "json_schema", "schema": OUTPUT_SCHEMA}, "effort": "medium"},
            )
        except Exception as exc:
            logger.warning("assistant call failed: %s", exc)
            raise AssistantError(f"Modellaufruf fehlgeschlagen: {exc}") from exc
        if response.stop_reason == "refusal":
            raise AssistantError("Das Modell hat die Anfrage abgelehnt.")
        if response.stop_reason != "end_turn":
            raise AssistantError(f"Antwort unvollständig (stop_reason={response.stop_reason}).")
        text = next((b.text for b in response.content if b.type == "text"), "")
        try:
            parsed = json.loads(text)
        except ValueError as exc:
            logger.warning("assistant response unparsable: %s", exc)
            raise AssistantError("Antwort des Modells war kein gültiges JSON.") from exc
        if not isinstance(parsed, dict):
            raise AssistantError("Antwort des Modells hatte ein unerwartetes Format.")
        result = AssistantResult(_text(parsed.get("reply"), MAX_TEXT), normalize_draft(parsed.get("draft"), candidates, draft), self.name)
        logger.info("assistant answered with %d markets (%s)", len(result.draft["markets"]), self.name)
        return result


def _candidate(e: CatalogEntry) -> dict:
    payload = entry_payload(e)
    payload["description"] = (e.description or "")[:300] or None
    return {k: payload[k] for k in ("id", "title", "description", "tags", "price", "liquidity", "volume_24h", "spread", "end_date")}


def assistant_from_env() -> Assistant:
    """Claude when an API key is configured, otherwise the keyword fallback (logged)."""
    if os.environ.get("ANTHROPIC_API_KEY"):
        return AnthropicAssistant(model=os.environ.get("MANUFACTURE_MODEL", MANUFACTURE_MODEL))
    logger.warning("ANTHROPIC_API_KEY not set: Index Manufacture uses the keyword fallback assistant")
    return KeywordAssistant()
