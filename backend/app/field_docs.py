"""Plain-language explanations of the market fields (single source for API and UI).

Each field names what it measures and how Polymarket and Kalshi raw data are
normalized into it, so both sources can be compared in USD. ``LIQUIDITY_BASES``
explains the per-market ``liquidity_basis`` tag set by the clients
(liquidity = 24h turnover in USD for both sources).
"""

from backend.app.clients.base import LIQUIDITY_BASIS_VOLUME_24H
from backend.app.eui.harmonize import SPREAD_LIMIT
from backend.app.models import MIN_LIQUIDITY_USD

FIELD_DOCS: list[dict] = [
    {
        "key": "price",
        "label": "Preis",
        "unit": "0–1",
        "what": "Preis eines Ja-Kontrakts, der bei Eintreten 1 USD auszahlt – also die vom Markt eingepreiste Wahrscheinlichkeit.",
        "polymarket": "Preis des Ja-Ausgangs (outcomePrices), bereits 0–1.",
        "kalshi": "Letzter Handelspreis, sonst bestes Ja-Gebot; alte Cent-Felder werden durch 100 geteilt.",
    },
    {
        "key": "liquidity",
        "label": "Liquidität ($)",
        "unit": "USD",
        "what": "Handelsumsatz der letzten 24 Stunden in USD – die Größe, nach der Märkte gefiltert, sortiert und in Indizes gewichtet werden. "
                "Ein Markt gilt als liquide, wenn dort gerade tatsächlich Geld umgesetzt wird.",
        "polymarket": "Gehandelter USD-Umsatz der letzten 24 h (volume24hr).",
        "kalshi": "In 24 h gehandelte Kontrakte × aktueller Preis (so wird aus Stückzahl ein Dollarbetrag).",
        "comparability": "Beide Quellen melden den 24h-Umsatz, beide Werte sind in USD und messen dasselbe – damit sind Polymarket und Kalshi direkt vergleichbar. "
                         "Früher genutzte Größen (Orderbuchtiefe bzw. Open Interest als Ersatz) waren das nicht.",
    },
    {
        "key": "volume",
        "label": "Volumen ($)",
        "unit": "USD",
        "what": "Gesamter Handelsumsatz seit Start des Marktes.",
        "polymarket": "Gehandelter USD-Umsatz (volumeNum).",
        "kalshi": "Anzahl gehandelter Kontrakte; da ein Kontrakt höchstens 1 USD auszahlt, entspricht das dem USD-Nennwert (Obergrenze des tatsächlich bezahlten Betrags).",
    },
    {
        "key": "volume_24h",
        "label": "24h ($)",
        "unit": "USD",
        "what": "Handelsumsatz der letzten 24 Stunden – zeigt, ob ein Markt gerade aktiv gehandelt wird.",
        "polymarket": "Gehandelter USD-Umsatz der letzten 24 h (volume24hr).",
        "kalshi": "In 24 h gehandelte Kontrakte × aktueller Preis (so wird aus Stückzahl ein Dollarbetrag).",
    },
    {
        "key": "spread",
        "label": "Spread",
        "unit": "Cent",
        "what": "Abstand zwischen bestem Verkaufs- und bestem Kaufangebot für „Ja“. Klein = enger, verlässlicher Preis.",
        "polymarket": "bestAsk − bestBid (bereits in USD).",
        "kalshi": "yes_ask − yes_bid in USD (alte Cent-Felder ÷ 100).",
    },
    {
        "key": "liquidity_score",
        "label": "Liquiditäts-Score",
        "unit": "USD",
        "what": f"Orderbuchtiefe (Polymarket; Kalshi: gemeldete Liquidität, sonst Open Interest) + 24h-Umsatz. Liegt der Spread über {SPREAD_LIMIT * 100:.0f} Cent, wird der Score anteilig gekürzt "
                f"(× {SPREAD_LIMIT * 100:.0f} ct / Spread). Grundlage der Gewichte im Economic Uncertainty Index.",
        "polymarket": "Aus den normalisierten Feldern berechnet – identische Formel für beide Quellen.",
        "kalshi": "Aus den normalisierten Feldern berechnet – identische Formel für beide Quellen.",
    },
    {
        "key": "min_liquidity",
        "label": "Mindestliquidität",
        "unit": "USD",
        "what": f"Märkte unter {MIN_LIQUIDITY_USD:,.0f} USD Liquidität (siehe oben) werden gar nicht erst geladen; Indizes können die Schwelle nur erhöhen.".replace(",", " "),
        "polymarket": "Gilt für den 24h-Umsatz.",
        "kalshi": "Gilt für den 24h-Umsatz (Kontrakte × Preis).",
    },
]

LIQUIDITY_BASES: dict[str, dict] = {
    LIQUIDITY_BASIS_VOLUME_24H: {"short": "", "label": "24h-Umsatz", "hint": "Handelsumsatz der letzten 24 Stunden (USD)."},
}


def field_docs_payload() -> dict:
    return {"fields": FIELD_DOCS, "liquidity_bases": LIQUIDITY_BASES}
