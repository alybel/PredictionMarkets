"""Classify directory markets for one risk topic: relevant yes/no, rationale and proposed risk reading.

Haiku gets the topic question and a batch of markets (id, title, tags) and answers in a JSON schema. Every answer is
checked: an unknown market id or a reading outside ``READINGS`` is discarded and logged as a warning. Without an API
key a keyword fallback (``model`` = ``keyword-fallback``) keeps the pipeline running and is marked as such.
"""

import json
import logging
import os
from dataclasses import dataclass, field
from typing import Protocol

from backend.app.risk.models import HIGH, LOW, READINGS, UNCERTAINTY

logger = logging.getLogger(__name__)

HAIKU_MODEL = "claude-haiku-5-5"
FALLBACK = "keyword-fallback"
BATCH_SIZE = 40
DOWN_WORDS = ("crash", "down", "below", "fall", "drop", "decline", "bear", "correction", "lose", "plunge", "recession")
UP_WORDS = ("above", "rise", "up ", "higher", "record", "all-time high", "gain")

SYSTEM_PROMPT = """You classify prediction markets for one risk topic of a risk index.
The input gives the topic (name and risk question) and markets (id, title, tags). For every market decide:
1. relevant: true if the market's outcome says something about the risk question, false otherwise.
2. rationale: one short sentence for the human reviewer.
3. reading - how the price p (probability of "Yes", 0..1) expresses the risk:
   "high": risk = p ("Yes" is the risky outcome, e.g. "S&P 500 down 20% this year?");
   "low": risk = 1 - p ("No" is the risky outcome, e.g. "S&P 500 above 6000 at year end?");
   "uncertainty": risk = 1 - 2*|p - 0.5| (neither answer is clearly risky; disagreement is the risk);
   "volatility": the risk lies in how strongly the price moves (e.g. exact ranges of an index level).
Use exactly the given ids. Return exactly one decision per input id."""

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {"decisions": {"type": "array", "items": {
        "type": "object",
        "properties": {"id": {"type": "string"}, "relevant": {"type": "boolean"}, "rationale": {"type": "string"},
                       "reading": {"type": "string", "enum": list(READINGS)}},
        "required": ["id", "relevant", "rationale", "reading"],
        "additionalProperties": False}}},
    "required": ["decisions"],
    "additionalProperties": False,
}


@dataclass(frozen=True)
class Item:
    market_id: int
    title: str
    tags: tuple[str, ...] = ()


@dataclass(frozen=True)
class Decision:
    market_id: int
    relevant: bool
    rationale: str
    reading: str
    model: str


@dataclass
class Result:
    decisions: list[Decision] = field(default_factory=list)
    discarded: int = 0  # answers with unknown id or invalid reading


class Classifier(Protocol):
    name: str

    def classify(self, topic: dict, items: list[Item]) -> Result: ...


def validate(raw, items: dict[str, Item], model: str) -> Result:
    """Turn raw answers into decisions; unknown ids and invalid readings are discarded and logged."""
    result = Result()
    for d in raw if isinstance(raw, list) else []:
        key = str(d.get("id")) if isinstance(d, dict) else None
        item = items.get(key) if key else None
        if item is None:
            logger.warning("risk classification: answer with unknown market id %r discarded (%s)", key, model)
            result.discarded += 1
        elif d.get("reading") not in READINGS:
            logger.warning("risk classification: market %s answer with invalid reading %r discarded (%s)", key, d.get("reading"), model)
            result.discarded += 1
        else:
            result.decisions.append(Decision(item.market_id, bool(d.get("relevant")), str(d.get("rationale", ""))[:512], d["reading"], model))
    return result


class KeywordClassifier:
    """Stand-in without API key: every pre-filtered market counts as relevant, the reading follows title keywords."""

    name = FALLBACK

    def classify(self, topic: dict, items: list[Item]) -> Result:
        raw = [{"id": str(i.market_id), "relevant": True, "rationale": "Stichwort-Regel (ohne KI)", "reading": self.reading(i.title)}
               for i in items]
        return validate(raw, {str(i.market_id): i for i in items}, self.name)

    @staticmethod
    def reading(title: str) -> str:
        t = f" {title.lower()} "
        if any(w in t for w in DOWN_WORDS):
            return HIGH
        return LOW if any(w in t for w in UP_WORDS) else UNCERTAINTY


class HaikuClassifier:
    """Batches markets to Haiku with a JSON-schema answer; failed batches are logged and left for the next run."""

    def __init__(self, client=None, model: str = HAIKU_MODEL, batch_size: int = BATCH_SIZE):
        if client is None:
            import anthropic

            client = anthropic.Anthropic()
        self._client, self.name, self.batch_size = client, model, batch_size

    def classify(self, topic: dict, items: list[Item]) -> Result:
        result = Result()
        for start in range(0, len(items), self.batch_size):
            part = self._batch(topic, items[start : start + self.batch_size])
            result.decisions += part.decisions
            result.discarded += part.discarded
        return result

    def _batch(self, topic: dict, batch: list[Item]) -> Result:
        by_id = {str(i.market_id): i for i in batch}
        payload = {"topic": topic, "markets": [{"id": k, "title": i.title, "tags": list(i.tags)} for k, i in by_id.items()]}
        try:
            response = self._client.messages.create(
                model=self.name, max_tokens=16000, system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
                output_config={"format": {"type": "json_schema", "schema": OUTPUT_SCHEMA}, "effort": "low"})
        except Exception as exc:  # network / API errors never abort the run
            logger.warning("risk classification batch failed (%d markets): %s", len(batch), exc)
            return Result()
        if response.stop_reason != "end_turn":
            logger.warning("risk classification batch stopped with %s (%d markets); left for the next run", response.stop_reason, len(batch))
            return Result()
        text = next((b.text for b in response.content if b.type == "text"), "")
        try:
            raw = json.loads(text)["decisions"]
        except (ValueError, KeyError, TypeError) as exc:
            logger.warning("risk classification answer unparsable (%d markets): %s", len(batch), exc)
            return Result(discarded=len(batch))
        return validate(raw, by_id, self.name)


def classifier_from_env() -> Classifier:
    """Haiku when an API key is configured, otherwise the marked keyword fallback (logged)."""
    if os.environ.get("ANTHROPIC_API_KEY"):
        return HaikuClassifier(model=os.environ.get("RISK_CLASSIFIER_MODEL", HAIKU_MODEL))
    logger.warning("ANTHROPIC_API_KEY not set: risk topics use the keyword fallback classifier")
    return KeywordClassifier()
