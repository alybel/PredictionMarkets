// Economic Uncertainty Index: public value + history chart, manual daily run and the
// human review of the model's topic/polarity proposals. Uses the globals $ and fmtNum of index.html.

const EUI_POLARITY_LABELS = { symmetric: "symmetrisch", bad_if_yes: "Ja = schlecht", bad_if_no: "Nein = schlecht" };
const EUI_JSON = { "Content-Type": "application/json" };

function euiStatus(text, isError = false) {
  const el = $("eui-status");
  el.textContent = text;
  el.classList.toggle("error", isError);
}

async function euiFetch(url, options) {
  const res = await fetch(url, options);
  if (!res.ok) {
    let detail = "HTTP " + res.status;
    try { detail = (await res.json()).detail || detail; } catch {}
    throw new Error(detail);
  }
  return res.json();
}

async function loadEui() {
  try {
    const [current, history] = await Promise.all([euiFetch("/eui"), euiFetch("/eui/history?days=180")]);
    $("eui-value").textContent = current.value == null ? "–" : current.value.toFixed(1);
    $("eui-date").textContent = current.value_date ? new Date(current.value_date).toLocaleDateString("de-DE") : "–";
    $("eui-count").textContent = current.constituent_count || "–";
    $("eui-as-of").textContent = current.as_of ? new Date(current.as_of).toLocaleString("de-DE") : "–";
    renderEuiChart(history);
    euiStatus(current.value == null ? "Noch kein Indexwert berechnet – Tageslauf starten." : "");
  } catch (err) {
    euiStatus("Index konnte nicht geladen werden: " + err.message, true);
  }
  loadEuiProposals();
  loadEuiConstituents();
}

function renderEuiChart(history) {
  const svg = $("eui-chart");
  svg.replaceChildren();
  const points = history.filter((h) => h.value != null);
  if (points.length < 1) return;
  const W = 600, H = 120, pad = 6;
  const x = (i) => (points.length === 1 ? W / 2 : pad + (i * (W - 2 * pad)) / (points.length - 1));
  const y = (v) => H - pad - (v / 100) * (H - 2 * pad);
  const ns = "http://www.w3.org/2000/svg";
  const path = document.createElementNS(ns, "path");
  path.setAttribute("d", points.map((p, i) => (i ? "L" : "M") + x(i).toFixed(1) + " " + y(p.value).toFixed(1)).join(" "));
  svg.appendChild(path);
  const last = points[points.length - 1];
  const dot = document.createElementNS(ns, "circle");
  dot.setAttribute("cx", x(points.length - 1)); dot.setAttribute("cy", y(last.value)); dot.setAttribute("r", 3);
  svg.appendChild(dot);
  const label = document.createElementNS(ns, "text");
  label.setAttribute("x", pad); label.setAttribute("y", 12);
  label.textContent = points[0].value_date + " – " + last.value_date + " (" + points.length + " Tage)";
  svg.appendChild(label);
}

// ---- review of proposals -------------------------------------------------------

async function loadEuiProposals() {
  let proposals = [];
  try { proposals = await euiFetch("/eui/admin/classifications?status=proposed&limit=100"); } catch { return; }
  const badge = $("eui-pending");
  badge.textContent = proposals.length + " offen";
  badge.classList.toggle("hidden", !proposals.length);
  $("eui-proposals-empty").classList.toggle("hidden", proposals.length > 0);
  const tbody = $("eui-proposals");
  tbody.replaceChildren();
  for (const p of proposals) tbody.appendChild(proposalRow(p));
}

function proposalRow(p) {
  const tr = document.createElement("tr");
  const select = document.createElement("select");
  for (const [value, text] of Object.entries(EUI_POLARITY_LABELS)) select.appendChild(new Option(text, value, false, value === p.polarity));
  const confirm = document.createElement("button");
  confirm.type = "button"; confirm.textContent = "Bestätigen";
  confirm.addEventListener("click", () => reviewProposal(p.id, { status: "confirmed", polarity: select.value }));
  const reject = document.createElement("button");
  reject.type = "button"; reject.textContent = "Ablehnen";
  reject.addEventListener("click", () => reviewProposal(p.id, { status: "rejected" }));
  const actions = document.createElement("div");
  actions.className = "row-actions";
  actions.append(confirm, reject);
  const cells = [[p.source, ""], [p.title, ""], [select, ""], [p.rationale || "–", "rationale"], [actions, ""]];
  for (const [content, cls] of cells) {
    const td = document.createElement("td");
    if (content instanceof Node) td.appendChild(content); else td.textContent = content;
    if (cls) td.className = cls;
    tr.appendChild(td);
  }
  return tr;
}

async function reviewProposal(id, body) {
  try {
    await euiFetch("/eui/admin/classifications/" + id, { method: "PUT", headers: EUI_JSON, body: JSON.stringify(body) });
    euiStatus("Zuordnung gespeichert – wirkt bei der nächsten Berechnung.");
    loadEuiProposals();
  } catch (err) {
    euiStatus("Speichern fehlgeschlagen: " + err.message, true);
  }
}

// ---- constituents + plausibility -----------------------------------------------

async function loadEuiConstituents() {
  try {
    const [data, plaus] = await Promise.all([euiFetch("/eui/admin/constituents"), euiFetch("/eui/plausibility")]);
    const tbody = $("eui-constituents");
    tbody.replaceChildren();
    for (const c of [...data.constituents].sort((a, b) => b.weight - a.weight)) {
      const tr = document.createElement("tr");
      const cells = [[c.title, ""], [c.price.toFixed(3), "num"], [EUI_POLARITY_LABELS[c.polarity] || c.polarity, ""],
        [c.contribution.toFixed(3), "num"], [(c.weight * 100).toFixed(1) + " %", "num"]];
      for (const [text, cls] of cells) {
        const td = document.createElement("td");
        td.textContent = text; if (cls) td.className = cls;
        tr.appendChild(td);
      }
      tbody.appendChild(tr);
    }
    const verdict = { plausible: "plausibel", implausible: "nicht plausibel", insufficient_data: "noch zu wenig Daten" }[plaus.verdict] || plaus.verdict;
    $("eui-plausibility").textContent =
      "Zusammensetzung " + (data.period || "–") + " · Historie " + plaus.history_days + " Tage" +
      (plaus.backtest_possible ? " · Backtest möglich" : " · Backtest ab " + plaus.min_history_days_for_backtest + " Tagen") +
      " · Plausibilitätscheck (Ereignistage höher als Umfeld): " + plaus.events_with_rise + "/" + plaus.events_checked + " → " + verdict;
  } catch (err) {
    $("eui-plausibility").textContent = "Details nicht verfügbar: " + err.message;
  }
}

// ---- manual run -------------------------------------------------------------------

$("eui-run").addEventListener("click", async () => {
  const btn = $("eui-run");
  btn.disabled = true;
  euiStatus("Tageslauf läuft: Märkte abrufen, zuordnen, berechnen … (kann eine Minute dauern)");
  try {
    const summary = await euiFetch("/eui/admin/run", { method: "POST" });
    const snap = summary.snapshot || {};
    euiStatus("Lauf abgeschlossen: Snapshot " + (snap.status || "–") + ", " + (summary.classified ?? 0) + " neue Zuordnungen, " + summary.index.constituents + " Bestandteile.");
    await loadEui();
  } catch (err) {
    euiStatus("Tageslauf fehlgeschlagen: " + err.message, true);
  } finally {
    btn.disabled = false;
  }
});

loadEui();
