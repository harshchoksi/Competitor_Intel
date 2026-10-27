"use strict";
const $ = (s, r = document) => r.querySelector(s);
const OKABE = ["#E69F00", "#56B4E9", "#009E73", "#CC79A7", "#D55E00", "#8c8c8c"]; // colour-blind friendly
const SOURCE_COLOR = "#2563eb";
const MAX_COMP = 5;
let pollTimer = null, charts = [], currentId = null, currentAnalysis = null;

// Safe DOM builder: scraped text is always inserted as text, never as HTML.
function h(tag, props = {}, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (v == null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat()) {
    if (kid == null || kid === false) continue;
    el.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
  }
  return el;
}
const inr = n => (n == null ? "–" : "₹" + Number(n).toLocaleString("en-IN", { maximumFractionDigits: 0 }));
const pct = (n, d = 1) => (n == null ? "–" : (n > 0 ? "+" : "") + n.toFixed(d) + "%");
const safeUrl = u => (/^https?:\/\//i.test(u || "") ? u : null);

// ---------- form ----------
function addCompetitor(value = "") {
  if (document.querySelectorAll(".comp-row").length >= MAX_COMP) return;
  const input = h("input", { type: "text", placeholder: "competitor.com", class: "comp-input" });
  input.value = value;
  const row = h("div", { class: "comp-row" }, input,
    h("button", { type: "button", class: "x", title: "Remove", "aria-label": "Remove competitor", onclick: () => { row.remove(); refreshCount(); } }, "✕"));
  $("#competitors").append(row);
  refreshCount();
}
function refreshCount() {
  const n = document.querySelectorAll(".comp-row").length;
  $("#comp-count").textContent = `(${n} of ${MAX_COMP})`;
  $("#add-comp").classList.toggle("hidden", n >= MAX_COMP);
}
function showError(msg) { const e = $("#form-error"); e.textContent = msg || ""; e.classList.toggle("hidden", !msg); }

$("#add-comp").addEventListener("click", () => addCompetitor());
// Threshold slider live display
$("#match-threshold").addEventListener("input", e => { $("#threshold-display").textContent = e.target.value + "%"; });
$("#demo").addEventListener("click", () => {
  $("#source").value = "localhost:8101";
  $("#competitors").innerHTML = "";
  [8102, 8103, 8104, 8105, 8106].forEach(p => addCompetitor("localhost:" + p));
});
$("#form").addEventListener("submit", async ev => {
  ev.preventDefault();
  showError("");
  const comps = [...document.querySelectorAll(".comp-input")].map(i => i.value.trim()).filter(Boolean);
  if (!$("#source").value.trim()) return showError("Enter your website.");
  if (!comps.length) return showError("Add at least one competitor.");
  const body = {
    source_url: $("#source").value.trim(), competitor_urls: comps,
    max_products: parseInt($("#max-products").value, 10) || 100,
    match_threshold: (parseInt($("#match-threshold").value, 10) || 85) / 100,
    categories: $("#categories").value.split(",").map(s => s.trim()).filter(Boolean),
    use_ai: $("#use-ai").checked,
  };
  $("#go").disabled = true;
  try {
    const res = await fetch("/api/analyses", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    const data = await res.json();
    if (!res.ok) throw new Error(typeof data.detail === "string" ? data.detail : "Please check the URLs you entered.");
    startWatching(data.id);
  } catch (e) { showError(e.message); } finally { $("#go").disabled = false; }
});

// ---------- polling & progress ----------
function startWatching(id) {
  currentId = id;
  history.replaceState(null, "", "#a=" + id);
  clearTimeout(pollTimer);
  charts.forEach(c => c.destroy()); charts = [];
  $("#results").classList.add("hidden");
  $("#progress-card").classList.remove("hidden");
  $("#progress-title").textContent = "Analyzing…";
  poll();
}
async function poll() {
  try {
    const res = await fetch("/api/analyses/" + currentId);
    if (!res.ok) throw new Error("Analysis not found");
    const a = await res.json();
    renderProgress(a);
    if (a.status === "done" || a.status === "failed") return finish(a);
  } catch (e) { $("#progress-title").textContent = e.message; return; }
  pollTimer = setTimeout(poll, 1500);
}
function renderProgress(a) {
  const sites = (a.progress && a.progress.sites) || {};
  const stage = a.progress && a.progress.stage;
  $("#progress-title").textContent = stage === "analyzing" ? "Comparing products…" : "Reading websites…";
  const list = $("#progress-list"); list.innerHTML = "";
  for (const s of Object.values(sites)) {
    list.append(h("li", {}, h("span", { class: "name" }, s.name, " ", s.role === "source" ? h("span", { class: "chip source" }, "you") : null),
      h("span", { class: "chip " + s.state }, s.state), h("span", { class: "msg" }, s.message)));
  }
}
function finish(a) {
  currentAnalysis = a;
  if (a.status === "failed") {
    $("#progress-title").textContent = "Analysis could not be completed";
    $("#progress-list").append(h("li", {}, h("span", { class: "error", style: "margin:0;flex:1" }, a.error || "Unknown error")));
    if (a.result) renderResults(a.result);
    return;
  }
  $("#progress-card").classList.add("hidden");
  renderResults(a.result);
  loadRecent();
}

// ---------- results ----------
function chart(canvas, cfg) {
  const c = new Chart(canvas, cfg); charts.push(c); return c;
}
function chartCard(title, note) {
  const canvas = h("canvas");
  return { el: h("div", { class: "card" }, h("h3", {}, title), note ? h("p", { class: "note" }, note) : null, h("div", { class: "chartbox" }, canvas)), canvas };
}
function renderResults(r) {
  const root = $("#results"); root.innerHTML = ""; root.classList.remove("hidden");
  const src = r.sites[r.source_key];
  const comps = r.competitors || [];
  const colorOf = {}; comps.forEach((c, i) => colorOf[c.key] = OKABE[i % OKABE.length]); colorOf[r.source_key] = SOURCE_COLOR;
  const totalMatched = comps.reduce((a, c) => a + c.matched, 0);
  const avgGap = comps.filter(c => c.matched).length ? comps.filter(c => c.matched).reduce((a, c) => a + c.avg_gap_pct, 0) / comps.filter(c => c.matched).length : null;
  const warnings = (r.alerts || []).filter(a => a.level === "warning").length;

  if (!r.comparison_available) { root.append(dataQuality(r)); return; }

  root.append(h("div", { class: "card toolbar", style: "justify-content:space-between" },
    h("span", {}, "Report ready. Includes all charts, alerts and appendices."),
    h("a", { class: "btn", href: "/api/analyses/" + currentId + "/report.pdf", download: "" }, "Download PDF report")));

  root.append(h("div", { class: "kpis" },
    kpi(src ? src.product_count : 0, "your products read"),
    kpi(totalMatched, "matched product comparisons"),
    kpi(avgGap == null ? "–" : pct(-avgGap), "your price vs competitors (avg; negative = you are cheaper)"),
    kpi(warnings, "price-undercut warnings")));

  const ai = aiCard(r); if (ai) root.append(ai);
  root.append(h("div", { class: "card" }, h("h2", {}, "Key insights"),
    h("ul", { class: "insights" }, (r.insights || []).map(i => h("li", {}, h("span", { class: "chip " + i.severity }, i.title), " ", i.text)))));

  if ((r.recommendations || []).length) {
    root.append(h("div", { class: "card" }, h("h2", {}, "Suggested actions"),
      h("ul", { class: "insights" }, r.recommendations.map(x => h("li", {}, h("span", { class: "chip " + (x.priority === "high" ? "failed" : x.priority === "medium" ? "warning" : "info") }, x.priority), " ", h("b", {}, x.action), h("br"), h("small", { style: "color:var(--muted)" }, x.detail))))));
  }

  const grid = h("div", { class: "grid2" });
  const withMatches = comps.filter(c => c.matched);
  const c1 = chartCard("Price index vs you", "Average competitor price where you = 100. Below 100 means the competitor is cheaper.");
  const c2 = chartCard("Cheaper, same or costlier than you", "Number of matched products by price position of each competitor.");
  const allSites = Object.values(r.sites);
  const c3 = chartCard("Products on sale", "Share of each catalogue with a visible discount, and the average discount depth.");
  const c4 = chartCard("Stock status", "Unknown means the site doesn't publish stock. We never guess.");
  grid.append(c1.el, c2.el, c3.el, c4.el); root.append(grid);

  chart(c1.canvas, { type: "bar", data: { labels: withMatches.map(c => c.name),
      datasets: [{ label: "Price index", data: withMatches.map(c => c.price_index), backgroundColor: withMatches.map(c => colorOf[c.key]) }] },
    options: { maintainAspectRatio: false, plugins: { legend: { display: false } }, scales: { y: { suggestedMin: 80, suggestedMax: 120, title: { display: true, text: "You = 100" } } } } });
  chart(c2.canvas, { type: "bar", data: { labels: withMatches.map(c => c.name), datasets: [
      { label: "Cheaper than you", data: withMatches.map(c => c.cheaper), backgroundColor: "#D55E00" },
      { label: "About the same (±1%)", data: withMatches.map(c => c.same), backgroundColor: "#9aa5b8" },
      { label: "Costlier than you", data: withMatches.map(c => c.costlier), backgroundColor: "#009E73" }] },
    options: { maintainAspectRatio: false, scales: { x: { stacked: true }, y: { stacked: true, title: { display: true, text: "Products" } } } } });
  chart(c3.canvas, { type: "bar", data: { labels: allSites.map(s => s.name), datasets: [
      { label: "% of products on sale", data: allSites.map(s => s.on_sale_pct || 0), backgroundColor: allSites.map(s => colorOf[s.key] || "#999") },
      { label: "Avg discount %", data: allSites.map(s => s.avg_discount_pct || 0), backgroundColor: "#8c8c8c" }] },
    options: { maintainAspectRatio: false, scales: { y: { title: { display: true, text: "%" } } } } });
  chart(c4.canvas, { type: "bar", data: { labels: allSites.map(s => s.name), datasets: [
      { label: "In stock", data: allSites.map(s => s.in_stock), backgroundColor: "#009E73" },
      { label: "Out of stock", data: allSites.map(s => s.out_of_stock), backgroundColor: "#D55E00" },
      { label: "Unknown", data: allSites.map(s => s.unknown_stock), backgroundColor: "#9aa5b8" }] },
    options: { maintainAspectRatio: false, scales: { x: { stacked: true }, y: { stacked: true, title: { display: true, text: "Products" } } } } });

  root.append(priceTable(r, comps));
  const catComp = categoryCard(r, comps); if (catComp) root.append(catComp);
  root.append(changesCard(r, colorOf));
  const off = offersCard(r); if (off) root.append(off);
  root.append(marketingCard(r));
  root.append(alertsCard(r));
  root.append(monitorCard());
  if ((r.review_matches || []).length) root.append(reviewCard(r));
  root.append(dataQuality(r));
}
function kpi(v, label) { return h("div", { class: "kpi" }, h("b", {}, v), h("span", {}, label)); }

function priceTable(r, comps) {
  const withData = comps.filter(c => c.matched);
  let sortKey = "title", dir = 1, q = "";
  const body = h("tbody"), head = h("thead");
  const cheapestGap = row => Math.min(...Object.values(row.competitors).map(v => v.gap_pct));
  function draw() {
    let rows = r.rows.filter(x => x.source.title.toLowerCase().includes(q));
    rows = [...rows].sort((a, b) => {
      if (sortKey === "title") return dir * a.source.title.localeCompare(b.source.title);
      if (sortKey === "yours") return dir * ((a.source.price || 0) - (b.source.price || 0));
      if (sortKey === "gap") return dir * (cheapestGap(a) - cheapestGap(b));
      return 0;
    });
    body.innerHTML = "";
    rows.forEach(row => {
      const s = row.source;
      body.append(h("tr", {},
        h("td", { class: "title" }, safeUrl(s.url) ? h("a", { href: s.url, target: "_blank", rel: "noopener noreferrer" }, s.title) : s.title, h("small", {}, [row.brand, row.category].filter(Boolean).join(" · "))),
        h("td", { class: "num" + (s.in_stock === false ? " oos" : "") }, inr(s.price), s.discount_pct ? h("small", {}, `${s.discount_pct}% off`) : null),
        withData.map(c => {
          const v = row.competitors[c.key];
          if (!v) return h("td", { class: "num" }, "–");
          const cls = v.gap_pct < -1 ? "cheap" : v.gap_pct > 1 ? "dear" : "";
          return h("td", { class: "num " + cls + (v.in_stock === false ? " oos" : ""), title: v.title + " (match " + Math.round(v.confidence * 100) + "%)" },
            inr(v.price), h("small", {}, pct(v.gap_pct), v.low_stock ? " · low stock" : "", v.in_stock === false ? " · out of stock" : ""));
        }),
        h("td", {}, row.cheapest ? (comps.find(c => c.key === row.cheapest) || {}).name : h("span", { class: "chip positive" }, "You"))));
    });
    if (!rows.length) body.append(h("tr", {}, h("td", { colspan: 3 + withData.length }, "No matching products.")));
  }
  const th = (label, key) => h("th", { class: key ? "sortable" : "", onclick: key ? () => { dir = sortKey === key ? -dir : 1; sortKey = key; draw(); } : null }, label + (key ? " ⇅" : ""));
  head.append(h("tr", {}, th("Product (yours)", "title"), th("Your price", "yours"), withData.map(c => h("th", {}, c.name)), th("Cheapest", "gap")));
  const search = h("input", { type: "text", placeholder: "Filter products…", "aria-label": "Filter products", oninput: e => { q = e.target.value.toLowerCase(); draw(); } });
  draw();
  return h("div", { class: "card" }, h("h2", {}, `Price comparison (${r.rows.length} matched products)`),
    h("p", { class: "legend" }, "Percent shows the competitor's price relative to yours. ",
      h("span", { class: "chip failed" }, "red = cheaper than you"), " ", h("span", { class: "chip done" }, "green = costlier than you"), " Hover a price to see the matched product."),
    h("div", { class: "toolbar" }, search), h("div", { class: "tablewrap" }, h("table", {}, head, body)));
}
function alertsCard(r) {
  const levels = ["all", "warning", "risk", "opportunity", "info"];
  let active = "all";
  const list = h("div"), bar = h("div", { class: "toolbar" });
  function draw() {
    list.innerHTML = "";
    const items = (r.alerts || []).filter(a => active === "all" || a.level === active).slice(0, 60);
    items.forEach(a => list.append(h("div", { class: "alert" }, h("span", { class: "chip " + a.level }, a.level), h("span", {}, a.message))));
    if (!items.length) list.append(h("p", { class: "note" }, "Nothing here."));
  }
  levels.forEach(l => bar.append(h("button", { type: "button", class: "ghost", onclick: () => { active = l; draw(); } },
    l + (l === "all" ? ` (${(r.alerts || []).length})` : ` (${(r.alerts || []).filter(a => a.level === l).length})`))));
  draw();
  return h("div", { class: "card" }, h("h2", {}, "Alerts"), bar, list);
}
function reviewCard(r) {
  return h("div", { class: "card" }, h("h2", {}, "Possible matches to double-check"),
    h("p", { class: "note" }, "These pairs look similar but we weren't confident enough to include them in the comparison."),
    h("div", { class: "tablewrap" }, h("table", {}, h("thead", {}, h("tr", {}, ["Competitor", "Your product", "Their product", "Confidence"].map(x => h("th", {}, x)))),
      h("tbody", {}, r.review_matches.slice(0, 30).map(m => h("tr", {}, h("td", {}, m.competitor), h("td", { class: "title" }, m.source_title, h("small", {}, inr(m.source_price))),
        h("td", { class: "title" }, m.competitor_title, h("small", {}, inr(m.competitor_price))), h("td", {}, Math.round(m.confidence * 100) + "%")))))));
}
function dataQuality(r) {
  return h("div", { class: "card" }, h("h2", {}, "Data quality"),
    h("p", { class: "note" }, "What we could and couldn't read from each site. Missing data is shown as missing, never guessed."),
    h("div", { class: "tablewrap" }, h("table", {}, h("thead", {}, h("tr", {}, ["Site", "Role", "Status", "Platform", "Method", "Products", "Details"].map(x => h("th", {}, x)))),
      h("tbody", {}, r.data_quality.map(d => h("tr", {}, h("td", {}, d.name), h("td", {}, d.role),
        h("td", {}, h("span", { class: "chip " + (d.status === "ok" ? "done" : "failed") }, d.status)), h("td", {}, d.platform || "–"), h("td", {}, d.method || "–"),
        h("td", { class: "num" }, d.products), h("td", { style: "white-space:normal" }, d.error || ""))))),
    ), r.coverage && Object.keys(r.coverage).length ? h("p", { class: "note" }, "Coverage: " + Object.values(r.coverage).map(c => `${c.name} matched ${c.source_products_matched}/${c.source_products} of your products`).join("; ")) : null);
}


// ---------- Category Comparison card (brand-vs-brand) ----------
function categoryCard(r, comps) {
  const catData = r.category_comparisons || {};
  // flatten: [{comp, category, ...}]
  const rows = [];
  comps.forEach(c => {
    (catData[c.key] || []).forEach(m => rows.push({ comp: c.name, compKey: c.key, ...m }));
  });
  if (!rows.length) return null;

  const gap = n => n == null ? "–" : (n > 0 ? "+" : "") + n.toFixed(1) + "%";
  const gapCls = n => n == null ? "" : n < -1 ? "cheap" : n > 1 ? "dear" : "";

  const tbody = h("tbody");
  rows.forEach(m => {
    tbody.append(h("tr", {},
      h("td", {}, m.category),
      h("td", {}, comps.length > 1 ? m.comp : null),
      h("td", { class: "num" }, m.source_count),
      h("td", { class: "num" }, m.comp_count),
      h("td", { class: "num" }, inr(m.source_avg_price)),
      h("td", { class: "num" }, inr(m.comp_avg_price)),
      h("td", { class: "num " + gapCls(m.price_gap_pct) }, gap(m.price_gap_pct),
        h("small", {}, m.price_gap_pct != null ? (m.price_gap_pct > 0 ? " (they're pricier)" : " (they're cheaper)") : "")),
      h("td", { class: "num" }, m.source_on_sale_pct != null ? m.source_on_sale_pct + "% / " + (m.comp_on_sale_pct ?? "–") + "%" : "–"),
    ));
  });

  return h("div", { class: "card" },
    h("h2", {}, "Category comparison"),
    h("p", { class: "note" }, "Compares average prices across shared product categories — useful when both brands sell similar items with different names."),
    h("div", { class: "tablewrap" }, h("table", {},
      h("thead", {}, h("tr", {},
        ["Category", comps.length > 1 ? "Competitor" : null, "Your products", "Their products", "Your avg price", "Their avg price", "Price gap", "On sale (yours / theirs)"].filter(Boolean).map(x => h("th", {}, x)))),
      tbody)));
}

// ---------- AI summary, changes, offers, marketing, monitors ----------
function aiCard(r) {
  const ai = r.ai || {};
  if (ai.status === "failed") return h("div", { class: "card" }, h("p", { class: "note" }, "AI summary unavailable (" + (ai.error || "unknown error") + "). Showing rule-based insights instead."));
  if (ai.status !== "ok") return null;
  const sev = x => (x === "warning" ? "warning" : x === "positive" ? "positive" : "info");
  return h("div", { class: "card aibox" }, h("h2", {}, "AI summary"),
    h("p", { class: "note" }, `Written by ${ai.model} from the numbers in this report. ${ai.removed_statements || 0} statement(s) containing numbers not found in the data were removed. Check before acting.`),
    ai.executive_summary ? h("p", {}, ai.executive_summary) : null,
    h("ul", { class: "insights" }, (ai.key_findings || []).map(f => h("li", {}, h("span", { class: "chip " + sev(f.severity) }, f.title), " ", f.text))),
    (ai.recommended_actions || []).length ? h("div", {}, h("h3", {}, "Recommended actions"),
      h("ul", { class: "insights" }, ai.recommended_actions.map(a => h("li", {}, h("span", { class: "chip " + (a.priority === "high" ? "failed" : a.priority === "medium" ? "warning" : "info") }, a.priority), " ", h("b", {}, a.action), a.rationale ? h("small", { style: "display:block;color:var(--muted)" }, a.rationale) : null)))) : null,
    (ai.risks || []).length ? h("div", {}, h("h3", {}, "Risks"), h("ul", {}, ai.risks.map(x => h("li", {}, x)))) : null,
    Object.keys(ai.per_competitor_notes || {}).length ? h("div", {}, h("h3", {}, "Notes per competitor"), h("ul", {}, Object.entries(ai.per_competitor_notes).map(([k, v]) => h("li", {}, h("b", {}, k + ": "), v)))) : null);
}

function siteName(r, key) { return (r.sites[key] || {}).name || (r.data_quality.find(d => d.key === key) || {}).name || key; }
function orderedKeys(r) { return [r.source_key, ...r.competitors.map(c => c.key)]; }

function changesCard(r, colorOf) {
  const ch = r.changes || {};
  if (!ch.has_history) return h("div", { class: "card" }, h("h2", {}, "Changes since last analysis"),
    h("p", { class: "note" }, "This is the first analysis of these sites. Run it again later, or start a monitor below, to see price changes, new products and new offers."));
  const sites = ch.sites || {};
  const rows = orderedKeys(r).filter(k => sites[k]);
  const drops = [];
  rows.forEach(k => sites[k].price_drops.forEach(d => drops.push({ ...d, site: siteName(r, k) })));
  drops.sort((a, b) => a.change_pct - b.change_pct);
  const card = h("div", { class: "card" }, h("h2", {}, "Changes since last analysis"),
    h("p", { class: "note" }, "Compared with the previous analysis of the same sites (" + new Date(ch.previous_run_at + "Z").toLocaleString() + ")."),
    h("div", { class: "tablewrap" }, h("table", {}, h("thead", {}, h("tr", {}, ["Site", "New products", "Removed", "Price drops", "Price rises", "Went out of stock", "New offers"].map(x => h("th", {}, x)))),
      h("tbody", {}, rows.map(k => { const c = sites[k]; return h("tr", {}, h("td", {}, siteName(r, k)), h("td", { class: "num" }, c.new_count), h("td", { class: "num" }, c.removed_count),
        h("td", { class: "num" }, c.price_drop_count), h("td", { class: "num" }, c.price_increase_count), h("td", { class: "num" }, c.went_out_of_stock.length), h("td", { title: c.new_offers.join("\n") }, c.new_offers.length)); })))),
    drops.length ? h("div", {}, h("h3", { style: "margin-top:14px" }, "Biggest price drops"),
      h("div", { class: "tablewrap" }, h("table", {}, h("tbody", {}, drops.slice(0, 8).map(d => h("tr", {}, h("td", {}, d.site), h("td", { class: "title" }, d.title), h("td", { class: "num" }, inr(d.old) + " → " + inr(d.new)), h("td", { class: "num cheap" }, pct(d.change_pct)))))))) : null);
  const ph = r.price_history;
  if (ph && ph.labels.length >= 2 && ph.products.length) {
    const canvas = h("canvas"), sel = h("select", { "aria-label": "Product" }, ph.products.map((p, i) => h("option", { value: i }, p.title)));
    const draw = () => {
      if (card._chart) { card._chart.destroy(); charts = charts.filter(c => c !== card._chart); }
      const p = ph.products[sel.value];
      card._chart = chart(canvas, { type: "line", data: { labels: ph.labels, datasets: Object.entries(p.series).map(([k, vals]) => ({ label: siteName(r, k), data: vals, borderColor: colorOf[k] || "#888", backgroundColor: colorOf[k] || "#888", borderWidth: k === r.source_key ? 3 : 1.6, spanGaps: true, tension: 0.15 })) },
        options: { maintainAspectRatio: false, scales: { y: { title: { display: true, text: "Price (₹)" } } } } });
    };
    sel.addEventListener("change", draw);
    card.append(h("h3", { style: "margin-top:14px" }, "Price history"), h("div", { class: "toolbar" }, sel), h("div", { class: "chartbox" }, canvas));
    setTimeout(draw, 0);
  }
  return card;
}

function offersCard(r) {
  const sig = r.signals || {};
  const keys = orderedKeys(r).filter(k => sig[k]);
  if (!keys.length) return null;
  const ship = g => (g.free_shipping_all ? "All orders" : g.free_shipping_threshold ? "Above " + inr(g.free_shipping_threshold) : "Not found");
  return h("div", { class: "card" }, h("h2", {}, "Offers, shipping and policies"),
    h("p", { class: "note" }, "Read from each homepage and its return, shipping and warranty pages. “Not found” means we did not see it, not that it does not exist."),
    h("div", { class: "tablewrap" }, h("table", {}, h("thead", {}, h("tr", {}, ["Site", "Free shipping", "Returns", "Warranty", "Payments", "Offers and codes"].map(x => h("th", {}, x)))),
      h("tbody", {}, keys.map(k => { const g = sig[k], pol = g.policies || {};
        return h("tr", {}, h("td", {}, siteName(r, k), k === r.source_key ? h("span", { class: "chip source", style: "margin-left:6px" }, "you") : null), h("td", {}, ship(g)),
          h("td", {}, pol.return_window_days ? pol.return_window_days + " days" : "Not found"), h("td", {}, pol.warranty || "Not found"),
          h("td", { style: "white-space:normal" }, (g.payment_methods || []).slice(0, 4).join(", ") || "Not found"),
          h("td", { style: "white-space:normal;min-width:240px" }, (g.offers || []).length ? g.offers.slice(0, 3).map(o => h("div", {}, o.code ? h("span", { class: "tag" }, o.code) : null, o.text.slice(0, 90))) : "None found")); })))));
}

function marketingCard(r) {
  const sig = r.signals || {}, social = r.social || {}, links = r.ad_links || {};
  const keys = orderedKeys(r).filter(k => sig[k] || social[k] || links[k]);
  if (!keys.length) return h("div");
  const ytText = (k, sl) => { const y = (social[k] || {}).youtube;
    if (y && y.status === "ok") return [y.followers ? y.followers.toLocaleString() + " subscribers" : "Subscribers hidden", y.posts_last_30d != null ? " · " + y.posts_last_30d + " videos / 30 days" : "", y.engagement_rate_pct != null ? " · " + y.engagement_rate_pct + "% engagement" : ""].join("");
    if (y) return y.error || "Not collected";
    return sl.youtube ? "Linked, not collected (set YOUTUBE_API_KEY)" : "No channel on homepage"; };
  return h("div", { class: "card" }, h("h2", {}, "Marketing and social"),
    h("p", { class: "note" }, "Ad pixels and tags suggest paid campaigns or retargeting but do not prove ads are live. Spend and creatives are not collected. Meta's official API does not cover commercial ads in India, so the ad library links open the free public libraries (India) for you to review."),
    h("div", { class: "tablewrap" }, h("table", {}, h("thead", {}, h("tr", {}, ["Site", "Tags found", "Social profiles", "YouTube", "Public ad libraries"].map(x => h("th", {}, x)))),
      h("tbody", {}, keys.map(k => { const g = sig[k] || {}, st = g.marketing_stack || {}, sl = g.social_links || {}, al = links[k] || {};
        return h("tr", {}, h("td", {}, siteName(r, k)),
          h("td", { style: "white-space:normal;min-width:200px" }, (st.advertising || []).map(t => h("span", { class: "tag ad" }, t)), [...(st.analytics || []), ...(st.engagement || []), ...(st.checkout || [])].map(t => h("span", { class: "tag" }, t)), !Object.keys(st).length ? "None found" : null),
          h("td", { style: "white-space:normal" }, Object.entries(sl).length ? Object.entries(sl).map(([p, v]) => safeUrl(v.url) ? h("a", { href: v.url, target: "_blank", rel: "noopener noreferrer", style: "margin-right:8px" }, p) : p) : "None found"),
          h("td", { style: "white-space:normal" }, ytText(k, sl)),
          h("td", {}, safeUrl(al.meta) ? h("a", { href: al.meta, target: "_blank", rel: "noopener noreferrer" }, "Meta") : null, " ", safeUrl(al.google) ? h("a", { href: al.google, target: "_blank", rel: "noopener noreferrer" }, "Google") : null)); })))));
}

function monitorCard() {
  const a = currentAnalysis;
  if (!a || a.status !== "done") return h("div");
  const interval = h("select", { "aria-label": "How often" }, [[6, "Every 6 hours"], [24, "Every day"], [168, "Every week"]].map(([v, t]) => h("option", { value: v, selected: v === 24 }, t)));
  const email = h("input", { type: "text", placeholder: "you@example.com (optional)", "aria-label": "Alert email" });
  const tg = h("input", { type: "text", placeholder: "Telegram chat id (optional)", "aria-label": "Telegram chat id" });
  const msg = h("p", { class: "note" });
  const go = h("button", { type: "button", class: "primary", onclick: async () => {
    go.disabled = true; msg.textContent = "";
    try {
      const res = await fetch("/api/monitors", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({
        source_url: a.source_url, competitor_urls: a.competitor_urls, interval_hours: parseInt(interval.value, 10), notify_email: email.value.trim() || null, telegram_chat_id: tg.value.trim() || null,
        max_products: parseInt($("#max-products").value, 10) || 100, categories: $("#categories").value.split(",").map(s => s.trim()).filter(Boolean), use_ai: $("#use-ai").checked }) });
      const data = await res.json();
      if (!res.ok) throw new Error(typeof data.detail === "string" ? data.detail : (data.detail && data.detail[0] && data.detail[0].msg) || "Could not start monitoring.");
      msg.textContent = "Monitoring started. The first run is in progress; it appears in the Monitors list above.";
      loadMonitors();
    } catch (e) { msg.textContent = e.message; } finally { go.disabled = false; }
  } }, "Start monitoring");
  return h("div", { class: "card" }, h("h2", {}, "Monitor this comparison"),
    h("p", { class: "note" }, "Re-run it automatically and get alerted about price drops, new offers, new products and new undercuts. Runs only while this website is running. Email and Telegram need server settings (see README)."),
    h("div", { class: "formrow" }, h("label", {}, "How often", h("br"), interval), h("label", {}, "Email alerts", h("br"), email), h("label", {}, "Telegram chat id", h("br"), tg), go), msg);
}

async function loadMonitors() {
  try {
    const items = await (await fetch("/api/monitors")).json();
    const card = $("#monitors-card"), box = $("#monitors");
    card.classList.toggle("hidden", !items.length);
    box.innerHTML = "";
    items.forEach(m => {
      const last = m.analyses[0];
      const act = (label, fn) => h("button", { type: "button", class: "ghost", onclick: async () => { await fn(); loadMonitors(); } }, label);
      box.append(h("div", { class: "mon" },
        h("b", {}, m.name), " ", h("span", { class: "chip " + (m.enabled ? "done" : "waiting") }, m.enabled ? "active" : "paused"),
        h("div", { class: "meta" }, `${m.source_url.replace(/^https?:\/\//, "")} vs ${m.competitor_count} · every ${m.interval_hours === 168 ? "week" : m.interval_hours === 24 ? "day" : m.interval_hours + " h"} · last run ${m.last_run_at ? new Date(m.last_run_at + "Z").toLocaleString() : "never"} · next ${m.enabled && m.next_run_at ? new Date(m.next_run_at + "Z").toLocaleString() : "–"}`),
        last ? h("div", { class: "meta" }, "Latest: ", h("a", { href: "#a=" + last.id, onclick: () => setTimeout(() => startWatching(last.id), 0) }, last.status), ` · ${last.alerts} alert(s)`,
          (last.notifications || []).length ? " · notifications: " + last.notifications.map(n => `${n.channel} ${n.status}`).join(", ") : "") : null,
        h("div", { class: "row-actions" }, act("Run now", () => fetch(`/api/monitors/${m.id}/run`, { method: "POST" })), act(m.enabled ? "Pause" : "Resume", () => fetch(`/api/monitors/${m.id}/toggle`, { method: "POST" })),
          act("Delete", () => { if (confirm("Delete this monitor? Past analyses are kept.")) return fetch(`/api/monitors/${m.id}`, { method: "DELETE" }); }))));
    });
  } catch { /* non-critical */ }
}

// ---------- boot ----------
async function loadRecent() {
  try {
    const items = await (await fetch("/api/analyses")).json();
    const box = $("#recent"); box.innerHTML = "";
    if (!items.length) return;
    box.append("Recent: ");
    items.slice(0, 5).forEach(i => box.append(h("a", { href: "#a=" + i.id, onclick: () => setTimeout(() => startWatching(i.id), 0) },
      i.source_url.replace(/^https?:\/\//, "") + " (" + i.status + ")")));
  } catch { /* non-critical */ }
}
(async function boot() {
  addCompetitor(); addCompetitor();
  try {
    const hres = await (await fetch("/api/health")).json();
    if (hres.allow_private_urls) $("#demo").classList.remove("hidden");
    if (hres.demo) $("#bump").classList.remove("hidden");
    if (!hres.ai_available) { $("#use-ai").checked = false; $("#use-ai").disabled = true; $("#ai-hint").textContent = "(add GEMINI_API_KEY on the server to enable)"; }
  } catch { /* ignore */ }
  $("#bump").addEventListener("click", async () => {
    const res = await fetch("/api/demo/bump", { method: "POST" });
    showError(res.ok ? "" : "Could not change demo prices.");
    if (res.ok) { $("#bump").textContent = "Prices changed. Click Analyze again to see the changes."; setTimeout(() => { $("#bump").textContent = "Demo: change competitor prices"; }, 6000); }
  });
  loadRecent(); loadMonitors(); setInterval(() => { if (!document.hidden) loadMonitors(); }, 20000);
  const m = location.hash.match(/#a=([a-f0-9]+)/);
  if (m) startWatching(m[1]);
})();
