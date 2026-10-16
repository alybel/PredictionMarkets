"""Pre-screening per market: proposed by Haiku, confirmed by a human.

For every market the model explains how the question expresses economic
uncertainty, proposes how the price is transformed to 0 (no uncertainty) ..
1 (high uncertainty) - the polarity - and, for range or threshold markets,
reads the range against the current reference value (increase / decrease).

Every decision is stored once in ``eui_classifications`` and reused on all
later runs; it is never re-decided by the model (a human may edit it).
Markets tagged with unmistakably non-economic labels (sports, pop culture)
are settled by a tag rule without a model call. Without an API key a
keyword fallback keeps the pipeline running and is marked as such.
"""

import json
import logging
import os
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy import select, tuple_
from sqlalchemy.orm import Session

from backend.app.eui.models import (
    DIRECTION_NA,
    DIRECTIONS,
    POLARITIES,
    POLARITY_BAD_IF_NO,
    POLARITY_BAD_IF_YES,
    POLARITY_SYMMETRIC,
    STATUS_PROPOSED,
    MarketClassification,
)
from backend.app.eui.reference import reference_payload

logger = logging.getLogger(__name__)

CLAUDE_MODEL = "claude-haiku-4-5"
BATCH_SIZE = 40
NON_ECONOMIC_TAGS = {
    "sports", "esports", "nba", "nfl", "mlb", "nhl", "mls", "soccer", "football", "basketball", "baseball",
    "hockey", "tennis", "golf", "f1", "ufc", "boxing", "chess", "olympics", "pop culture", "entertainment",
    "music", "movies", "tv", "gaming", "celebrities", "awards", "reality tv",
}
ECONOMIC_KEYWORDS = (
    "fed", "fomc", "interest rate", "rate cut", "rate hike", "inflation", "cpi", "pce", "gdp", "recession",
    "unemployment", "jobs report", "payroll", "tariff", "treasury", "yield", "s&p", "nasdaq", "dow", "stock",
    "oil", "gold", "debt ceiling", "shutdown", "default", "bank", "ecb", "boe", "bank of japan", "trade deal",
    "exports", "imports", "housing", "mortgage", "consumer", "earnings", "layoffs", "strike",
)
BAD_IF_YES_KEYWORDS = (
    "recession", "default", "shutdown", "above", "exceed", "higher than", "rise above", "crash", "layoff",
    "tariff", "unemployment", "bankrupt", "strike", "war", "collapse",
)

SYSTEM_PROMPT = """You pre-screen prediction-market questions for an Economic Uncertainty Index.
The index maps every market price p (probability of "Yes", 0..1) to an uncertainty contribution
between 0 (no uncertainty) and 1 (high uncertainty). For every question decide:
1. is_economic: true if the outcome is relevant for the economy or financial markets (monetary policy,
   inflation, labour market, growth, trade and tariffs, fiscal policy, commodities, equity and bond markets,
   banking, major corporate events, geopolitical events with clear economic impact). Sports, entertainment,
   celebrity, weather-for-fun and pure culture questions are false.
2. uncertainty_note: one sentence on how this market says something about economic uncertainty
   (what a high or low price would tell an economist).
3. polarity - the price transformation:
   "symmetric": contribution = 1 - 2*|p - 0.5|; use when neither answer is clearly bad for the economy
   (e.g. the exact size of a rate move) - disagreement itself is the uncertainty;
   "bad_if_yes": contribution = p; use when "Yes" is the adverse or destabilising outcome
   (recession, default, shutdown, surprise hike, unemployment above a threshold);
   "bad_if_no": contribution = 1 - p; use when "No" is the adverse outcome (debt ceiling raised in time).
4. direction: for questions about a numeric range or threshold of an indicator (policy rate, CPI,
   unemployment, yields, oil), compare the range with the current reference value supplied in the input:
   "increase" if the range lies above it, "decrease" if below, "unchanged" if it contains the current value,
   "n/a" if the question has no numeric range or no reference value is available. Use this reading when
   choosing the polarity (a range above the current policy rate is a hike, below is a cut).
5. reference_indicator: the indicator key you compared against, or "" when none.
6. rationale: one short sentence for the human reviewer.
Return exactly one decision per input id."""

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "decisions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "is_economic": {"type": "boolean"},
                    "uncertainty_note": {"type": "string"},
                    "polarity": {"type": "string", "enum": list(POLARITIES)},
                    "direction": {"type": "string", "enum": list(DIRECTIONS)},
                    "reference_indicator": {"type": "string"},
                    "rationale": {"type": "string"},
                },
                "required": ["id", "is_economic", "uncertainty_note", "polarity", "direction", "reference_indicator", "rationale"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["decisions"],
    "additionalProperties": False,
}


@dataclass(frozen=True)
class ClassificationInput:
    source: str
    external_id: str
    title: str
    tags: tuple[str, ...] = ()

    @property
    def key(self) -> tuple[str, str]:
        return (self.source, self.external_id)


@dataclass(frozen=True)
class Decision:
    source: str
    external_id: str
    is_economic: bool
    polarity: str
    rationale: str
    model: str
    uncertainty_note: str = ""
    direction: str = DIRECTION_NA
    reference_indicator: str | None = None
    reference_value: float | None = None


class Classifier(Protocol):
    name: str

    def classify(self, items: list[ClassificationInput], references: list[dict] = ()) -> list[Decision]: ...


class KeywordClassifier:
    """Rule-based stand-in when no language model is configured (marked in ``model``)."""

    name = "keyword-fallback"

    def classify(self, items: list[ClassificationInput], references: list[dict] = ()) -> list[Decision]:
        return [self._decide(item) for item in items]

    def _decide(self, item: ClassificationInput) -> Decision:
        title = item.title.lower()
        economic = any(k in title for k in ECONOMIC_KEYWORDS)
        polarity = POLARITY_BAD_IF_YES if any(k in title for k in BAD_IF_YES_KEYWORDS) else POLARITY_SYMMETRIC
        return Decision(item.source, item.external_id, economic, polarity, "keyword rule", self.name)


def output_config(model: str) -> dict:
    """Structured JSON output; ``effort`` only where the model accepts it (Haiku 4.5 rejects it)."""
    config: dict = {"format": {"type": "json_schema", "schema": OUTPUT_SCHEMA}}
    if "haiku" not in model:
        config["effort"] = "low"
    return config


class AnthropicClassifier:
    """Batches titles plus the current reference values to Claude and parses the JSON-schema answer."""

    def __init__(self, client=None, model: str = CLAUDE_MODEL, batch_size: int = BATCH_SIZE):
        if client is None:
            import anthropic

            client = anthropic.Anthropic()
        self._client = client
        self.name = model
        self.batch_size = batch_size

    def classify(self, items: list[ClassificationInput], references: list[dict] = ()) -> list[Decision]:
        decisions: list[Decision] = []
        for start in range(0, len(items), self.batch_size):
            decisions.extend(self._classify_batch(items[start : start + self.batch_size], list(references)))
        return decisions

    def _classify_batch(self, batch: list[ClassificationInput], references: list[dict]) -> list[Decision]:
        by_id = {f"{i.source}:{i.external_id}": i for i in batch}
        payload = {
            "reference_values": references,
            "markets": [{"id": key, "title": i.title, "tags": list(i.tags)} for key, i in by_id.items()],
        }
        try:
            response = self._client.messages.create(
                model=self.name,
                max_tokens=8000,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
                output_config=output_config(self.name),
            )
        except Exception as exc:  # network / API errors must never abort the daily run
            logger.warning("pre-screening batch failed (%d items): %s", len(batch), exc)
            return []
        if response.stop_reason != "end_turn":
            logger.warning("pre-screening batch stopped with %s; skipped", response.stop_reason)
            return []
        text = next((b.text for b in response.content if b.type == "text"), "")
        try:
            raw = json.loads(text)["decisions"]
        except (ValueError, KeyError, TypeError) as exc:
            logger.warning("pre-screening response unparsable: %s", exc)
            return []
        values = {r["indicator"]: r.get("value") for r in references}
        decisions = []
        for d in raw:
            if not isinstance(d, dict) or (item := by_id.get(str(d.get("id")))) is None:
                continue
            indicator = d.get("reference_indicator") or None
            decisions.append(
                Decision(
                    item.source, item.external_id, bool(d.get("is_economic")),
                    d.get("polarity") if d.get("polarity") in POLARITIES else POLARITY_SYMMETRIC,
                    str(d.get("rationale", ""))[:512], self.name,
                    uncertainty_note=str(d.get("uncertainty_note", ""))[:512],
                    direction=d.get("direction") if d.get("direction") in DIRECTIONS else DIRECTION_NA,
                    reference_indicator=str(indicator)[:64] if indicator else None,
                    reference_value=values.get(indicator) if indicator else None,
                )
            )
        return decisions


def classifier_from_env() -> Classifier:
    """Haiku when an API key is configured, otherwise the keyword fallback (logged)."""
    if os.environ.get("ANTHROPIC_API_KEY"):
        return AnthropicClassifier(model=os.environ.get("EUI_CLASSIFIER_MODEL", CLAUDE_MODEL))
    logger.warning("ANTHROPIC_API_KEY not set: using keyword fallback classifier")
    return KeywordClassifier()


def tag_rule(item: ClassificationInput) -> Decision | None:
    """Unmistakably non-economic tags settle the topic without a model call."""
    if any(t.lower() in NON_ECONOMIC_TAGS for t in item.tags):
        return Decision(item.source, item.external_id, False, POLARITY_SYMMETRIC, "non-economic tag", "tag-rule")
    return None


def classifications_for(session: Session, keys) -> dict[tuple[str, str], MarketClassification]:
    """Stored decisions for (source, external_id) keys."""
    keys = list({tuple(k) for k in keys})
    if not keys:
        return {}
    result: dict[tuple[str, str], MarketClassification] = {}
    for start in range(0, len(keys), 500):  # keep IN-lists small for SQLite
        chunk = keys[start : start + 500]
        stmt = select(MarketClassification).where(
            tuple_(MarketClassification.source, MarketClassification.external_id).in_(chunk)
        )
        for row in session.scalars(stmt):
            result[(row.source, row.external_id)] = row
    return result


def classify_missing(session: Session, markets, classifier: Classifier, limit: int | None = None) -> int:
    """Store decisions for markets without one (seen markets are never re-sent); returns the number of new rows."""
    items = [
        ClassificationInput(m.source, m.external_id, m.title, tuple(getattr(m, "tags", None) or []))
        for m in markets
    ]
    known = classifications_for(session, [i.key for i in items])
    pending = [i for i in items if i.key not in known]
    if limit is not None:
        pending = pending[:limit]
    decisions: list[Decision] = []
    for_model: list[ClassificationInput] = []
    for item in pending:
        ruled = tag_rule(item)
        decisions.append(ruled) if ruled else for_model.append(item)
    if for_model:
        references = reference_payload(session)
        if not references:
            logger.warning("no reference values stored: range markets cannot be read as increase/decrease")
        logger.info("pre-screening %d markets with %s (%d settled by tag rule, %d reference values)",
                    len(for_model), classifier.name, len(decisions), len(references))
        decisions.extend(classifier.classify(for_model, references))
    titles = {i.key: i.title for i in pending}
    for d in decisions:
        session.add(
            MarketClassification(
                source=d.source, external_id=d.external_id, title=titles[(d.source, d.external_id)][:512],
                is_economic=d.is_economic, polarity=d.polarity, rationale=d.rationale, model=d.model,
                uncertainty_note=d.uncertainty_note or None, direction=d.direction,
                reference_indicator=d.reference_indicator, reference_value=d.reference_value,
                status=STATUS_PROPOSED,
            )
        )
    session.commit()
    logger.info("stored %d new pre-screenings", len(decisions))
    return len(decisions)
