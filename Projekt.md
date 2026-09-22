# PredictionMarketIndex

_Angelegt am 2026-09-17 11:22 mit DevConsole._

## Idee
I want to get an on-demand overview on the most liquid markets with an impact on the Political and Economical global landscape. 

I specifically to not want to subscribe a specific channel but rather have a set of rules how these channels are chosen. (like an Index). 

There may be a political Index and a economical index, or a middle-east index, or an oil-index, or a market-fear index. 

These indizes get their data from Polymarket and Kalshi, have liquidity thresholds and present a consolidated view.

## Zielgruppe
Trader, Makro-Analysten und informierte Beobachter, die eine schnelle, konsolidierte Einschätzung politischer und wirtschaftlicher Marktstimmung suchen — ohne einzelne Prediction-Markets manuell zu verfolgen.

## Architektur
- **Plattform:** Web-App (on-demand Abruf, kein Dauer-Streaming)
- **Sprache:** Python-Backend (.venv), React/TypeScript-Frontend
- **Datenhaltung:** PostgreSQL für Markt-Snapshots und Index-Regeln, kein Langzeit-Streaming-Speicher nötig
- **Schnittstellen:** Polymarket API, Kalshi API, interne REST-API für das Frontend

## Core Features
- Regelbasierte Index-Definitionen (Kategorie/Keyword-Filter + Liquiditätsschwelle)
- Vordefinierte Indizes: Political, Economic, Middle-East, Oil, Market-Fear
- Konsolidierte Aggregation mehrerer Märkte zu einem Index-Wert
- On-Demand-Abruf statt Subscription auf einzelne Märkte

## MVP — erste Arbeitspakete
1. **Projekt-Gerüst und Datenmodell** — Legt Repo-Struktur, Backend-Skeleton und das Datenmodell für Märkte und Index-Regeln an, sodass alle folgenden Pakete darauf aufbauen können.
2. **Anbindung Polymarket und Kalshi** — Implementiert Clients, die Marktdaten (Preis, Volumen, Liquidität) von Polymarket und Kalshi abrufen und in ein gemeinsames Schema normalisieren.
3. **Index-Regelwerk-Engine** — Baut die Engine, die anhand von Kategorie/Keyword-Filtern und Liquiditätsschwelle bestimmt, welche Märkte zu welchem Index gehören.
4. **Konsolidierte Aggregation je Index** — Aggregiert die von der Regelwerk-Engine ausgewählten Märkte je Index zu einem konsolidierten Wert und stellt ihn über eine API bereit.
5. **On-Demand Dashboard** — Stellt eine Frontend-Ansicht bereit, über die ein Index per Klick abgerufen und der konsolidierte Wert samt Einzelmärkten angezeigt wird — der Kernnutzen ist damit Ende-zu-Ende erlebbar.
