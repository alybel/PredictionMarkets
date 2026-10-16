// Component attribution of the trade view (1015-1200): which index markets carry the correlation and the beta
// of the selected stock, split into price and weight effect
// (GET /indices/{name}/correlation/{ticker}/attribution, POST .../attribution/preview for unsaved rules).
// Relies on the globals of shell.js, builder.js, correlation.js ($, logEvent, state, builder, corr, signedPct, fmtCorr).

const atr = { request: 0 };
const fmtContribution = (v) => (v == null ? "–" : (v >= 0 ? "+" : "") + v.toFixed(3).replace(".", ","));

async function loadAttribution(ticker, rule) {
  const ticket = ++atr.request;
  $("cr-attr-rows").replaceChildren();
  $("cr-attr-caption").textContent = "Beiträge werden berechnet …";
  const url = corrUrl("/" + encodeURIComponent(ticker) + "/attribution" + (rule ? "/preview" : "")); // archive only
  try {
    const res = await fetch(url, rule
      ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(rule) }
      : undefined);
    if (!res.ok) throw new Error("HTTP " + res.status);
    const data = await res.json();
    if (ticket !== atr.request) return;
    renderAttribution(data);
    logEvent("info", "correlation.attribution.loaded", { ticker, n: data.n, components: data.component_count, preview: !!rule });
  } catch (err) {
    if (ticket !== atr.request) return;
    $("cr-attr-caption").textContent = "Beiträge konnten nicht geladen werden: " + err.message;
    logEvent("error", "correlation.attribution.failed", { ticker, message: err.message });
  }
}

function attrRow(label, r, cls, title) {
  const tr = document.createElement("tr");
  if (cls) tr.className = cls;
  const cells = [
    [label, "market"], [r.days_in_index ?? "", "num"],
    [r.avg_weight == null ? "" : (r.avg_weight * 100).toFixed(1).replace(".", ",") + " %", "num"],
    [fmtContribution(r.corr_price), "num sig-col"], [fmtContribution(r.corr_weight), "num"], [fmtContribution(r.corr_total), "num"],
    [signedPct(r.beta_price, 3), "num sig-col"], [signedPct(r.beta_weight, 3), "num"], [signedPct(r.beta_total, 3), "num"],
    [r.own_correlation === undefined ? "" : fmtCorr(r.own_correlation), "num sig-col"],
  ];
  for (const [text, c] of cells) {
    const td = document.createElement("td");
    td.textContent = text;
    td.className = c;
    tr.appendChild(td);
  }
  if (title) tr.firstChild.title = title;
  return tr;
}

function renderAttribution(d) {
  const body = $("cr-attr-rows");
  body.replaceChildren();
  for (const r of d.components) {
    const src = r.source === "kalshi" ? "Kalshi" : r.source === "polymarket" ? "Polymarket" : r.source || "";
    body.appendChild(attrRow(r.title, r, "", src + " · " + r.title + " · " + r.own_n + " Tage mit Beitrag"));
  }
  if (d.others) body.appendChild(attrRow("Übrige (" + d.others.count + " Märkte)", d.others, "others"));
  if (d.total) body.appendChild(attrRow("Summe (" + d.total.count + " Märkte)", d.total, "sum",
    "Entspricht der Korrelation " + fmtCorr(d.correlation) + " und dem Beta " + signedPct(d.slope_per_point, 3) + " der Haupttabelle"));
  $("cr-attr-caption").textContent = !d.total
    ? (d.n < d.min_observations
      ? "Zu wenige Paare (" + d.n + " von mindestens " + d.min_observations + ") – keine Zerlegung, wie in der Haupttabelle."
      : "Keine Zerlegung möglich (keine Schwankung in Index oder Aktie).")
    : "Die Index-Veränderung je Tag ist exakt in Preiseffekt (Gewicht gestern × Preisänderung) und Gewichtseffekt " +
      "(Gewichtsänderung × Preis heute, inkl. Ein- und Austritte) je Markt zerlegt; die Anteile summieren sich zur " +
      "Korrelation (" + fmtCorr(d.correlation) + ") und zum Beta (" + signedPct(d.slope_per_point, 3) + " je Index-Punkt) der Haupttabelle. " +
      "Gezeigt: Top " + d.components.length + " nach Betrag des Korrelations-Anteils über " + d.n + " Paare. " +
      "Achtung: Bei wenigen Index-Tagen spielt der Zufall eine große Rolle – deshalb gibt es bewusst keine p-Werte je Komponente." +
      (corr.rule ? " · Vorschau mit ungespeicherten Regeln" : "");
}
