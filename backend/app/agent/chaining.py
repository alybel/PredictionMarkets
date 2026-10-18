"""Chain detection: successive markets of the same recurring question form one long series.

Recurring questions ("Fed cut in March 2025?", "... in May 2025?") differ only in their period. Removing the
period (months, days, years, quarters, weeks, ISO dates) gives a title template; markets sharing a template
with at least two different periods are the links of a chain, ordered by end date. Each link is active
until its end date (roll), then the next link takes over. Like the Fed chains (``backend.app.fed.metrics``)
the daily change is measured within one link, so the roll causes no jump; gaps between links are reported,
never filled. A chain whose links all trade at once (deadline ladder, several meetings) has no roll inside its
stored history yet (``rolls`` = 0): it reaches no further back than its longest link. Thresholds and other numbers stay in
the template: "CPI above 3%" and "above 4%" never chain.
"""

import hashlib
import logging
import re
from dataclasses import dataclass, field
from datetime import date, timedelta

from backend.app.agent.pool import Candidate

logger = logging.getLogger(__name__)

PERIOD = "<period>"
MIN_LINKS = 2
_MONTHS = ("january|february|march|april|june|july|august|september|october|november|december|"
           "jan|feb|mar|apr|jun|jul|aug|sept|sep|oct|nov|dec")
_DAY = r"(?:\s+\d{1,2}(?:st|nd|rd|th)?)?"
_YEAR = r"(?:,?\s+(?:19|20)\d{2})?"
PATTERNS = [
    re.compile(r"\b\d{4}-\d{2}-\d{2}\b"),
    re.compile(r"\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b"),
    re.compile(rf"\b(?:{_MONTHS})\.?{_DAY}{_YEAR}\b"),
    re.compile(rf"\b(?<=in |by |of |on )may{_DAY}{_YEAR}\b"),
    re.compile(r"\bq[1-4](?:\s+(?:19|20)\d{2})?\b"),
    re.compile(r"\bweek\s+(?:of\s+)?\d{1,2}\b"),
    re.compile(r"\b(?:19|20)\d{2}\b"),
]


def template(title: str) -> str | None:
    """Title without its period, or None when the title names no period (then it cannot chain)."""
    text, found = title.lower(), False
    for pattern in PATTERNS:
        text, n = pattern.subn(PERIOD, text)
        found = found or n > 0
    if not found:
        return None
    text = re.sub(rf"{PERIOD}(?:[\s,/-]*{PERIOD})+", PERIOD, text)
    return re.sub(r"\s+", " ", text).strip()


def chain_key(tpl: str) -> str:
    return "chain:" + hashlib.sha1(tpl.encode()).hexdigest()[:12]


@dataclass
class Link:
    market: Candidate
    roll_on: date | None  # last day this link is the active one

    def payload(self) -> dict:
        c = self.market
        return {"id": c.key, "source": c.source, "external_id": c.external_id, "title": c.title, "days": c.days,
                "first_date": _iso(c.first_date), "last_date": _iso(c.last_date), "end_date": _iso(c.end_date),
                "roll_on": _iso(self.roll_on), "live": c.live}


@dataclass
class Chain:
    key: str
    label: str
    links: list[Link]
    gaps: list[dict] = field(default_factory=list)

    @property
    def sources(self) -> list[str]:
        return sorted({l.market.source for l in self.links})

    @property
    def first_date(self) -> date | None:
        return min((l.market.first_date for l in self.links if l.market.first_date), default=None)

    @property
    def last_date(self) -> date | None:
        return max((l.market.last_date for l in self.links if l.market.last_date), default=None)

    @property
    def rolls(self) -> int:
        """Roll-overs inside the stored history; 0 means the links trade in parallel (term structure, no history gain yet)."""
        last = self.last_date
        return sum(1 for l in self.links[:-1] if l.roll_on and last and l.roll_on <= last)

    @property
    def history_days(self) -> int:
        if not self.first_date:
            return 0
        return (self.last_date - self.first_date).days + 1 - sum(g["days"] for g in self.gaps)

    def payload(self) -> dict:
        return {"key": self.key, "label": self.label, "sources": self.sources, "link_count": len(self.links), "rolls": self.rolls,
                "first_date": _iso(self.first_date), "last_date": _iso(self.last_date), "history_days": self.history_days,
                "gaps": self.gaps, "links": [l.payload() for l in self.links]}


def _iso(d: date | None) -> str | None:
    return d.isoformat() if d else None


def _period_end(c: Candidate) -> date | None:
    return c.end_date or c.last_date


def _gaps(links: list[Link]) -> list[dict]:
    """Calendar days between a link's last stored day and the next link's first stored day."""
    gaps = []
    for prev, nxt in zip(links, links[1:]):
        end, start = prev.market.last_date, nxt.market.first_date
        if end and start and (start - end).days > 1:
            gaps.append({"from": (end + timedelta(days=1)).isoformat(), "to": (start - timedelta(days=1)).isoformat(),
                         "days": (start - end).days - 1})
    return gaps


def build_chains(candidates: list[Candidate]) -> list[Chain]:
    """Chains with at least two links of different periods, longest history first."""
    groups: dict[str, list[Candidate]] = {}
    for c in candidates:
        tpl = template(c.title)
        if tpl:
            groups.setdefault(tpl, []).append(c)
    chains = []
    for tpl, members in groups.items():
        if len({_period_end(c) for c in members}) < MIN_LINKS:
            continue
        ordered = sorted(members, key=lambda c: (_period_end(c) or date.max, c.source, c.external_id))
        links = [Link(c, _period_end(c)) for c in ordered]
        chains.append(Chain(chain_key(tpl), tpl.replace(PERIOD, "…"), links, _gaps(links)))
    chains.sort(key=lambda ch: (-(ch.rolls > 0), -ch.history_days, -len(ch.links), ch.label))
    logger.info("agent chains: %d chains from %d candidates (%d templates)", len(chains), len(candidates), len(groups))
    return chains
