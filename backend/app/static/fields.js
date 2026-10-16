// Field explanations from GET /fields: tooltips on table headers (th[data-doc]), the
// "Kennzahlen erklärt" panels (details[data-field-docs]) and the liquidity cell with its basis.

const fieldDocs = { fields: [], bases: {} };

// Liquidity value plus a marker when it rests on a proxy (e.g. Kalshi open interest).
function liquidityCell(m) {
  const span = document.createElement("span");
  span.textContent = fmtNum(m.liquidity);
  const basis = fieldDocs.bases[m.liquidity_basis];
  if (basis) span.title = basis.label + ": " + basis.hint;
  if (basis && basis.short) {
    const tag = document.createElement("sup");
    tag.className = "basis";
    tag.textContent = basis.short;
    span.appendChild(tag);
  }
  return span;
}

function fieldTooltip(doc) {
  return [doc.label + " (" + doc.unit + "): " + doc.what, "Polymarket: " + doc.polymarket, "Kalshi: " + doc.kalshi]
    .concat(doc.comparability ? ["Vergleichbarkeit: " + doc.comparability] : []).join("\n");
}

function renderFieldPanel(panel) {
  panel.replaceChildren();
  const summary = document.createElement("summary");
  summary.textContent = "ⓘ Kennzahlen erklärt – was ist Liquidität und wie werden Polymarket und Kalshi vergleichbar?";
  panel.appendChild(summary);
  const dl = document.createElement("dl");
  dl.className = "field-docs";
  for (const doc of fieldDocs.fields) {
    const dt = document.createElement("dt");
    dt.textContent = doc.label + " · " + doc.unit;
    const dd = document.createElement("dd");
    for (const [label, text] of [["", doc.what], ["Polymarket", doc.polymarket], ["Kalshi", doc.kalshi], ["Vergleichbarkeit", doc.comparability]]) {
      if (!text) continue;
      const p = document.createElement("p");
      if (label) { const b = document.createElement("strong"); b.textContent = label + ": "; p.appendChild(b); }
      p.appendChild(document.createTextNode(text));
      dd.appendChild(p);
    }
    dl.append(dt, dd);
  }
  panel.appendChild(dl);
  panel.addEventListener("toggle", () => logEvent("info", "fields.panel.toggle", { panel: panel.id, open: panel.open }));
}

async function loadFieldDocs() {
  try {
    const res = await fetch("/fields");
    if (!res.ok) throw new Error("HTTP " + res.status);
    const data = await res.json();
    fieldDocs.fields = data.fields || [];
    fieldDocs.bases = data.liquidity_bases || {};
    const byKey = Object.fromEntries(fieldDocs.fields.map((d) => [d.key, d]));
    document.querySelectorAll("th[data-doc]").forEach((th) => {
      const doc = byKey[th.dataset.doc];
      if (doc) { th.title = fieldTooltip(doc); th.classList.add("has-doc"); }
    });
    document.querySelectorAll("details[data-field-docs]").forEach(renderFieldPanel);
    // Tables drawn before the docs arrived get their liquidity markers now.
    try { if (typeof window.renderTable === "function") window.renderTable(); } catch (err) { /* table not loaded yet */ }
    logEvent("info", "fields.loaded", { count: fieldDocs.fields.length });
  } catch (err) {
    logEvent("warning", "fields.load_failed", { error: String(err) });
  }
}

loadFieldDocs();
