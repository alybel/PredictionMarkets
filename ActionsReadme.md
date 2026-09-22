# GitHub-Actions-Testlauf für Marktdaten

Einwöchiger Versuch: Sechs feste Märkte (drei Kalshi, drei Polymarket) werden alle ~5 Minuten abgerufen, jeder Lauf schreibt eine JSON-Datei, einmal pro Stunde landen die Stände als Tagesdatei im Repository. Ziel ist die Erkenntnis, ob Zeitplan, Abruf und Rückschreiben eine Woche ohne Eingriff laufen. Fehler werden protokolliert, nicht verhindert.

## Aufbau

| Baustein | Datei | Aufgabe |
|---|---|---|
| Marktauswahl | `probe/markets.json` | Sechs Kennungen, von Hand gewählt (Leitzins, CPI, Arbeitslosenquote je Plattform) |
| Sammler | `backend/app/probe/collect.py` | Zwei Sammelabfragen (Kalshi `tickers=`, Polymarket Gamma `id=`), eine JSON-Datei je Lauf |
| Zusammenführung | `backend/app/probe/merge.py` | Holt die Laufdateien (Workflow-Artefakte), hängt sie an `probe/data/<Tag>.jsonl` an, schreibt `<Tag>.status.json` |
| Workflow 1 | `.github/workflows/probe-collect.yml` | `*/5 * * * *`, kein Commit, Laufdatei als Artefakt `probe-run-<run_id>-<attempt>` (7 Tage) |
| Workflow 2 | `.github/workflows/probe-commit.yml` | `7 * * * *`, Merge und genau ein Commit pro Stunde |

Die Dollar-Umrechnung ist Schritt 1 der Liquiditätsharmonisierung (`backend/app/eui/harmonize.py`): Kalshi-Cent werden durch 100 geteilt, 24h-Kontrakte mal Preis ergeben den Umsatz, Orderbuchtiefe ist die gemeldete Liquidität in USD (bei Kalshi als Ersatz das Open Interest, weil die öffentliche API dort 0 meldet).

## Einrichtung (einmalig)

Das Projekt ist derzeit noch kein Git-Repository. Reihenfolge:

1. **Repository anlegen und pushen**
   ```bash
   cd ~/TechProjects/PredictionMarketIndex
   git init -b main
   git add .
   git commit -m "PredictionMarketIndex mit GitHub-Actions-Probe"
   gh repo create PredictionMarketIndex --public --source=. --push
   ```
   `.gitignore` hält `.venv`, `logs/`, `backend/dev.db`, `.devconsole/` und `probe/runs/` fern. Prüfe vor dem ersten Push, ob `FeatureMap.yaml`, `Discussions.yaml` und `BugReports.yaml` öffentlich sein dürfen; sonst ebenfalls in `.gitignore` aufnehmen.

2. **Öffentlich, nicht privat.** 288 Läufe am Tag kosten rund 1 200 Actions-Minuten im Monat (jeder Lauf zählt aufgerundet als volle Minute). Private Repositories haben 2 000 freie Minuten, Zusammenführungs-Läufe und Neuversuche kommen dazu. Öffentliche Repositories laufen unbegrenzt frei.

3. **Schreibrecht für Workflows.** GitHub → Settings → Actions → General → „Workflow permissions" → „Read and write permissions". Die Workflows fordern die Rechte zusätzlich per `permissions:`-Block an.

4. **Ersten Lauf von Hand starten.** Actions → „Probe collect" → „Run workflow", danach „Probe commit" → „Run workflow". Die Zeitpläne greifen anschließend automatisch.

## Was entsteht

Je Lauf eine Datei `run-<UTC-Zeit>-<run_id>.json` (als Artefakt), je Tag eine Datei `probe/data/2026-09-23.jsonl` (eine Zeile pro Lauf) und `probe/data/2026-09-23.status.json`:

```json
{"runs": 286, "ok": 280, "partial": 5, "failed": 1, "http_429": 2, "source_errors": 6,
 "gaps": [{"from": "2026-09-23T03:05:00+00:00", "to": "2026-09-23T03:25:00+00:00", "minutes": 20.0}],
 "first": "2026-09-23T00:02:11+00:00", "last": "2026-09-23T23:57:40+00:00"}
```

Ein Laufdatensatz enthält `timestamp`, `run_id`, `status` (`ok`, `partial`, `failed`), je Plattform `http_status`, `elapsed_ms`, `error` und je Markt `platform`, `market_id`, `topic`, `title`, `price`, `best_bid`, `best_ask`, `spread`, `depth_usd`, `volume_24h_usd`, `active`, `end_date`, `error`.

## Woran man Probleme erkennt

- **Roter Lauf in Actions:** Der Sammler beendet sich mit Exit-Code 1, sobald ein Markt oder eine Quelle fehlt. Die Laufdatei wird trotzdem hochgeladen (`if: always()`).
- **`http_429`** in der Statusdatei zählt Abrufgrenzen, **`gaps`** listet Abstände über 10 Minuten (GitHub verschiebt Zeitpläne bei Last um einige Minuten, das ist normal; größere Lücken sind ausgefallene Läufe).
- **Fehlende Commits:** „Probe commit" ist idempotent und holt bis zu 48 Stunden Artefakte nach. Ein ausgefallener Stunden-Lauf verliert also keine Daten.

## Grenzen des Versuchs

- Der Takt ist ungefähr fünf Minuten, nicht exakt. Zeitpläne laufen nur auf dem Standard-Branch und werden nach 60 Tagen ohne Aktivität deaktiviert.
- 24 Commits am Tag lösen bei Deploy Now ebenso viele Bauvorgänge aus. Stört das, gehört `probe/data/` in ein eigenes Datenrepository oder Deploy Now baut nur einen anderen Branch.
- Drei der gewählten Märkte enden früh (`KXU3-26SEP-T3.9` und Polymarket `4217155` am 2026-10-02, `KXCPI-26SEP-T0.4` am 2026-10-14). Nach dem Ende liefern die Quellen sie als inaktiv oder gar nicht mehr; beides steht in der Laufdatei. Bei Bedarf `probe/markets.json` anpassen und committen.
- Artefakte verfallen nach 7 Tagen; die Tagesdateien im Repository sind die dauerhafte Ablage.

## Lokal ausführen

```bash
.venv/bin/python -m backend.app.probe.collect --out probe/runs          # ein Lauf, Datei unter probe/runs/
.venv/bin/python -m backend.app.probe.merge --local probe/runs           # Laufdateien in probe/data/ zusammenführen
.venv/bin/python -m pytest backend/tests/test_probe_collect.py backend/tests/test_probe_merge.py backend/tests/test_probe_workflows.py
```

Logs landen wie beim Rest der Anwendung in `logs/app.log`.

## Nach einer Woche

Trägt der Weg (kaum Lücken, keine Handgriffe nötig), wird der Takt auf eine Stunde gesenkt und die Marktauswahl regelbasiert. Hält er nicht, wandern die Snapshots in eine gehostete Datenbank wie Supabase.
