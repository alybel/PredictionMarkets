"""Fixed, executable rule catalog of chain and single-market metrics (agent step 2).

Only these rules are ever executed by ``metrics.chain``. Rules the model writes in free text, or keys outside
the catalog, never reach the calculation: ``split`` turns them into notes ("Hinweis, nicht automatisch umgesetzt").
Gap and expiry rules are always on (the engine never fills a value), the roll rule is always on for chains.
"""

import logging

logger = logging.getLogger(__name__)

DIRECTION = "direction"          # per link reading (READINGS): up = p, down = 1 - p, uncertainty, volatility
ROLL_END_DATE = "roll_end_date"  # a link is active up to and including its end date, then the next one
FRONT_CHAIN = "front_chain"      # parallel links: per day the nearest-due link that has a price (constant time to expiry)
NO_GAP_FILL = "no_gap_fill"      # days without a price of the active link stay empty
NO_FORWARD_FILL = "no_forward_fill"  # after a link's end date (and after the last link) nothing is carried forward

RULES: dict[str, dict] = {
    DIRECTION: {"label": "Richtung", "text": "Richtung je Glied: steigend = p, fallend = 1 − p; weitere Lesarten: Unsicherheit = 1 − 2·|p − 0,5|, "
                                            "Volatilität = Schwankung der Tagesänderungen über 20 Handelstage."},
    ROLL_END_DATE: {"label": "Roll am Enddatum", "text": "Jedes Glied ist bis einschließlich seines Enddatums aktiv, danach das nächste; "
                                                        "die Tagesänderung wird nur innerhalb eines Glieds gemessen (kein Sprung am Roll-Tag)."},
    FRONT_CHAIN: {"label": "Front-Kette / konstante Restlaufzeit", "text": "Bei parallel gehandelten Gliedern je Tag das nächstfällige "
                                                                          "Glied, das an diesem Tag einen Preis hat."},
    NO_GAP_FILL: {"label": "Lücken nicht auffüllen", "text": "Tage ohne Preis des aktiven Glieds bleiben leer."},
    NO_FORWARD_FILL: {"label": "Ablauf ohne Vorwärtsauffüllen", "text": "Nach dem Enddatum eines Glieds zählen seine Preise nicht mehr; "
                                                                       "nach dem letzten Glied endet die Reihe."},
}
UP, DOWN, UNCERTAINTY, VOLATILITY = "up", "down", "uncertainty", "volatility"
VOL_WINDOW = 20  # volatility: sample standard deviation of the last 20 within-link daily changes; fewer -> no value
READINGS: dict[str, dict] = {
    UP: {"label": "steigend (p)", "text": "Wahrscheinlichkeit p."},
    DOWN: {"label": "fallend (1 − p)", "text": "Gegenwahrscheinlichkeit 1 − p."},
    UNCERTAINTY: {"label": "Unsicherheit", "text": "1 − 2·|p − 0,5|: 1 bei p = 0,5, 0 bei p = 0 oder 1."},
    VOLATILITY: {"label": "Volatilität", "text": f"Standardabweichung der Tagesänderungen von p über {VOL_WINDOW} Handelstage, nur innerhalb "
                                                 "eines Glieds (kein Roll-Sprung); kürzeres Fenster liefert keinen Wert."},
}
ALWAYS = (DIRECTION, NO_GAP_FILL, NO_FORWARD_FILL)
CHAIN_ALWAYS = (ROLL_END_DATE,)
NOTE_LIMIT = 8
NOTE_LENGTH = 300


def split(keys, free_text, kind: str, context: str = "") -> tuple[list[str], list[str]]:
    """(executable rule keys in catalog order, notes); unknown keys and free text become notes, logged as warning."""
    wanted = {str(k).strip() for k in keys or [] if str(k).strip()}
    unknown = sorted(k for k in wanted if k not in RULES)
    rules = set(ALWAYS) | (set(CHAIN_ALWAYS) if kind == "chain" else set()) | (wanted & set(RULES))
    if kind != "chain":
        rules -= {ROLL_END_DATE, FRONT_CHAIN}
    notes = [f"Unbekannte Regel „{k[:60]}“" for k in unknown]
    notes += [str(t).strip()[:NOTE_LENGTH] for t in free_text or [] if str(t).strip()]
    if unknown or notes:
        logger.warning("chain rules %s: %d rule(s) outside the catalog kept as note only: %s", context or "-", len(notes), unknown or notes[:3])
    return [k for k in RULES if k in rules], notes[:NOTE_LIMIT]


def reading(value) -> str:
    """Stored reading of a link; anything unknown reads as ``up`` (the former default)."""
    return value if value in READINGS else UP


def metric_reading(links) -> str:
    """Reading of a metric: the readings of its links in link order, joined (normally one)."""
    return ", ".join(dict.fromkeys(reading(l.direction) for l in links))


def readings_payload() -> list[dict]:
    return [{"key": k, **v} for k, v in READINGS.items()]


def payload() -> list[dict]:
    return [{"key": k, **v} for k, v in RULES.items()]
