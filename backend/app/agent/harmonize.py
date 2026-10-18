"""Harmonization rules of a blueprint, derived from its components (deterministic, no model needed).

The rules say how differently sourced, timed and shaped series become one comparable index: one price
scale, one direction, chained levels without roll jumps, gaps left open, one daily timestamp across
sources, normalized multi-outcome events and a common start date.
"""

from collections import Counter

DOWN = "down"


def rules(components: list[dict], event_sizes: dict[str, int]) -> list[dict]:
    """[{topic, rule}] for the resolved components (see ``blueprint.resolve``)."""
    out = [{"topic": "Preisskala", "rule": "Alle Preise als Wahrscheinlichkeit 0–1 lesen (Kalshi-Cent sind beim Laden bereits durch 100 "
                                           "geteilt, Polymarket liefert 0–1); Volumen und Liquidität in USD."}]
    if any(c["direction"] == DOWN for c in components):
        out.append({"topic": "Richtung", "rule": "Bausteine mit Richtung „fallend“ gehen als 1 − p ein, damit ein steigender Index "
                                                 "überall dasselbe bedeutet."})
    chains = [c for c in components if c["kind"] == "chain"]
    if chains:
        out.append({"topic": "Verkettung", "rule": "Tagesänderung immer innerhalb eines Kettenglieds messen; am Roll-Tag (Enddatum des "
                                                   "alten Glieds) übernimmt das nächste Glied, das verkettete Niveau springt nicht."})
        out.append({"topic": "Restlaufzeit", "rule": "Kurz vor der Auflösung laufen Preise gegen 0 oder 1. Für ein gleichbleibendes "
                                                     "Signal das Folgeglied übernehmen, sobald es gehandelt wird (konstante Laufzeit)."})
    if any(c["rolls"] == 0 for c in chains):
        out.append({"topic": "Laufzeitstruktur", "rule": "Glieder ohne bisherigen Roll laufen parallel (verschiedene Fristen gleichzeitig): "
                                                         "je Tag das nächstfällige Glied nehmen (Front-Kette); die Historie reicht dann nur so weit wie das längste Glied."})
    gaps = [g for c in chains for g in c["gaps"]]
    if gaps:
        out.append({"topic": "Lücken", "rule": f"{len(gaps)} Lücke(n) zwischen Kettengliedern ({sum(g['days'] for g in gaps)} Tage): "
                                               "nicht auffüllen; der Baustein fällt dort aus der Gewichtung."})
    sources = {s for c in components for s in c["sources"]}
    if len(sources) > 1:
        out.append({"topic": "Quellen", "rule": "Kalshi und Polymarket auf denselben Tagesstand (täglicher Snapshot, UTC) ausrichten; "
                                                "dieselbe Frage auf beiden Plattformen mitteln statt doppelt zählen."})
    events = Counter(m["event_key"] for c in components for m in c["markets"] if m.get("event_key"))
    if any(event_sizes.get(e, n) > 1 for e, n in events.items()):
        out.append({"topic": "Mehrere Outcomes", "rule": "Outcome-Preise eines Events vor der Verwendung auf 100 % normieren oder als "
                                                         "Erwartungswert lesen, sonst zählt ein Ereignis mehrfach."})
    starts = sorted(c["first_date"] for c in components if c["first_date"])
    if len(starts) > 1 and starts[0] != starts[-1]:
        out.append({"topic": "Startdatum", "rule": f"Gemeinsame Historie erst ab {starts[-1]} (frühester Baustein ab {starts[0]}): "
                                                   "Gewichte bei Eintritt eines Bausteins neu auf 100 % normieren."})
    out.append({"topic": "Gewichtung", "rule": "Gewichte summieren auf 100 %; der Liquiditätsfilter wirkt nur am aktuellen Rand, "
                                               "nicht rückwirkend auf die Historie."})
    return out


def merge(fixed: list[dict], extra: list[dict]) -> list[dict]:
    """Fixed rules first; the model's rules only for topics the fixed rules do not cover."""
    topics = {r["topic"].lower() for r in fixed}
    added = [r for r in extra if r.get("topic") and r.get("rule") and r["topic"].lower() not in topics]
    return fixed + [{"topic": str(r["topic"])[:64], "rule": str(r["rule"])[:600]} for r in added[:8]]
