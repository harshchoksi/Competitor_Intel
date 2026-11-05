"use strict";
// Pricing recommendations tab: risk/opportunity banners, product-type analysis, charts and the paginated product explorer.
// Uses helpers from app.js (h, $, money, pct, safeUrl, chart, chartCard, currentId, charts).

const ACTION_LABEL = { decrease: "Reduce price", increase: "Raise price", hold: "Keep price" };
const ACTION_CLASS = { decrease: "failed", increase: "done", hold: "info" };

function pricingTab(r) {
  facets = null; ctl = {}; Object.assign(F, { q: "", category: "", brand: "", competitor: "", action: "", min_price: "", max_price: "", min_similarity: 0, in_stock: "", sort: "gap_desc", page: 1 });
  const p = r.pricing || {}, s = p.summary || {}, box = h("div");
  const opt = p.settings || {};
  box.append(h("div", { class: "kpis" },
    kpi((s.matched_products || 0).toLocaleString(), `of your ${(s.source_products || 0).toLocaleString()} products matched (${s.coverage_pct ?? 0}% coverage)`),
    kpi((r.competitors || []).length, "competitors compared"),
    kpi((s.competitor_matches || 0).toLocaleString(), "similar competitor products found"),
    kpi(s.avg_match_confidence == null ? "–" : s.avg_match_confidence + "%", "average match confidence"),
    kpi(s.cheapest_count || 0, "products where you are already cheapest")));

  // --- risk + opportunity
  const risk = h("div", { class: "card banner risk" }), opp = h("div", { class: "card banner opp" });
  risk.append(h("div", { class: "eyebrow" }, "Price risk"),
    h("h2", {}, `${(s.decrease || 0).toLocaleString()} products are priced above the competitor average`),
    h("p", { class: "note" }, `Combined gap per unit: ${money(s.overpriced_gap_total)}. Suggested: lower each to about ${opt.undercut_pct ?? 1}% under the competitor average.`),
    h("div", { id: "risk-list" }, h("p", { class: "note" }, "Loading…")));
  opp.append(h("div", { class: "eyebrow" }, "Opportunity"),
    h("h2", {}, `${(s.increase || 0).toLocaleString()} products are priced well below the competitor average`),
    h("p", { class: "note" }, `Headroom per unit: ${money(s.headroom_total)}. Suggested: raise by up to ${opt.max_increase_pct ?? 5}%, staying under the market.`),
    h("div", { id: "opp-list" }, h("p", { class: "note" }, "Loading…")));
  box.append(h("div", { class: "grid2" }, risk, opp));
  fillTop("decrease", "gap_desc", "#risk-list", it => `Cut ${money(-it.change_abs)}`);
  fillTop("increase", "gap_asc", "#opp-list", it => `Raise ${money(it.change_abs)}`);

  // --- product-type analysis
  const cats = p.categories || [];
  if (cats.length) {
    const body = h("tbody");
    cats.forEach(c => body.append(h("tr", { class: "clickrow", title: "Click to filter the products below", onclick: () => { F.category = c.category; F.page = 1; syncControls(); loadExplorer(); document.getElementById("explorer").scrollIntoView({ behavior: "smooth" }); } },
      h("td", {}, h("b", {}, c.category)), h("td", { class: "num" }, c.products), h("td", { class: "num " + (c.avg_gap_pct > 3 ? "cheap" : c.avg_gap_pct < -3 ? "dear" : "") }, pct(c.avg_gap_pct)),
      h("td", { class: "num" }, money(c.avg_your_price)), h("td", { class: "num" }, money(c.avg_market_price)),
      h("td", { class: "num" }, c.decrease + (c.avg_decrease_pct != null ? ` (${pct(c.avg_decrease_pct)})` : "")), h("td", { class: "num" }, c.increase + (c.avg_increase_pct != null ? ` (${pct(c.avg_increase_pct)})` : "")),
      h("td", { style: "white-space:normal" }, c.verdict))));
    box.append(h("div", { class: "card" }, h("h2", {}, "Matched products by type"),
      h("p", { class: "note" }, "Products are grouped by what they are (meat, dairy, drinks…), even when stores name them differently. Gap = your average price vs the competitor average for the same products. Click a row to see its products."),
      h("div", { class: "tablewrap" }, h("table", {}, h("thead", {}, h("tr", {}, ["Product type", "Matched", "Your gap", "Your avg price", "Market avg", "Reduce", "Raise", "Advice"].map(x => h("th", {}, x)))), body))));
  }

  // --- charts
  const g = h("div", { class: "grid2" });
  const ca = chartCard("Price position", "Share of matched products priced above, in line with, or below the competitor average.");
  const cb = chartCard("Match confidence", "How sure we are that competitor products are the same as yours.");
  const cc = chartCard("Average gap by product type", "Positive = you are more expensive than competitors.");
  const cd = chartCard("Matches per competitor", "How many of your products each competitor also sells.");
  g.append(ca.el, cb.el, cc.el, cd.el); box.append(g);
  chart(ca.canvas, { type: "doughnut", data: { labels: ["Above average", "In line", "Below average"], datasets: [{ data: [s.above || 0, s.aligned || 0, s.below || 0], backgroundColor: ["#ef4444", "#a8896e", "#10b981"] }] }, options: { maintainAspectRatio: false } });
  const order = ["<80", "80-85", "85-90", "90-95", "95-100"], bk = s.confidence_buckets || {};
  chart(cb.canvas, { type: "bar", data: { labels: order.map(o => o + "%"), datasets: [{ label: "Matches", data: order.map(o => bk[o] || 0), backgroundColor: "#f59e0b" }] }, options: { maintainAspectRatio: false, plugins: { legend: { display: false } } } });
  const topCats = cats.slice(0, 12);
  chart(cc.canvas, { type: "bar", data: { labels: topCats.map(c => c.category), datasets: [{ label: "Avg gap %", data: topCats.map(c => c.avg_gap_pct), backgroundColor: topCats.map(c => c.avg_gap_pct > 0 ? "#ef4444" : "#10b981") }] }, options: { indexAxis: "y", maintainAspectRatio: false, plugins: { legend: { display: false } } } });
  const names = {}; (r.competitors || []).forEach(c => names[c.key] = c.name);
  const mp = s.matches_per_competitor || {};
  chart(cd.canvas, { type: "bar", data: { labels: Object.keys(mp).map(k => names[k] || k), datasets: [{ label: "Products", data: Object.values(mp), backgroundColor: OKABE }] }, options: { maintainAspectRatio: false, plugins: { legend: { display: false } } } });

  box.append(explorerCard(r, p));
  setTimeout(loadExplorer, 0); // the tab is attached to the page after this function returns
  return box;
}

async function fillTop(action, sort, sel, label) {
  try {
    const d = await (await fetch(`/api/analyses/${currentId}/pricing?action=${action}&sort=${sort}&page_size=8`)).json();
    const box = $(sel); if (!box) return; box.innerHTML = "";
    if (!d.items.length) return box.append(h("p", { class: "note" }, "None. Nothing to do here."));
    d.items.forEach(it => box.append(h("div", { class: "toprow" }, thumb(it), h("div", { class: "grow" }, h("b", {}, it.title), h("small", {}, `Market avg ${money(it.market_avg)} · your price ${money(it.price)} (${pct(it.gap_pct)})`)),
      h("span", { class: "chip " + ACTION_CLASS[action] }, label(it)))));
  } catch { /* non-critical */ }
}
const thumb = it => safeUrl(it.image) ? h("img", { class: "thumb", src: it.image, alt: "", loading: "lazy", referrerpolicy: "no-referrer" }) : h("div", { class: "thumb ph" }, (it.title || "?")[0]);

// ---------- explorer ----------
const F = { q: "", category: "", brand: "", competitor: "", action: "", min_price: "", max_price: "", min_similarity: 0, in_stock: "", sort: "gap_desc", page: 1, page_size: 20 };
let facets = null, ctl = {};

function explorerCard(r, p) {
  const sel = (id, label, opts) => { const el = h("select", { id, "aria-label": label, onchange: e => { F[id.replace("f-", "")] = e.target.value; F.page = 1; loadExplorer(); } }, opts.map(([v, t]) => h("option", { value: v }, t))); ctl[id] = el; return el; };
  const text = (id, ph, key, type = "text") => { const el = h("input", { id, type, placeholder: ph, "aria-label": ph, oninput: debounce(e => { F[key] = e.target.value; F.page = 1; loadExplorer(); }, 300) }); ctl[id] = el; return el; };
  const comps = [["", "All competitors"], ...(r.competitors || []).map(c => [c.key, c.name])];
  const bar = h("div", { class: "filters" },
    text("f-q", "Search product, brand, SKU…", "q"),
    sel("f-category", "Product type", [["", "All product types"], ...((p.categories || []).map(c => [c.category, `${c.category} (${c.products})`]))]),
    sel("f-brand", "Brand", [["", "All brands"]]),
    sel("f-competitor", "Competitor", comps),
    sel("f-action", "Recommended action", [["", "Any action"], ["decrease", "Reduce price"], ["increase", "Raise price"], ["hold", "Keep price"]]),
    sel("f-in_stock", "Availability", [["", "Any availability"], ["yes", "Not out of stock"], ["no", "Out of stock"]]),
    sel("f-min_similarity", "Minimum match", [["0", "Any match %"], ["80", "≥ 80%"], ["90", "≥ 90%"], ["95", "≥ 95%"]]),
    text("f-min_price", "Min price", "min_price", "number"), text("f-max_price", "Max price", "max_price", "number"),
    sel("f-sort", "Sort", [["gap_desc", "Most overpriced first"], ["gap_asc", "Most underpriced first"], ["change_desc", "Biggest price change"], ["price_desc", "Price: high to low"], ["price_asc", "Price: low to high"], ["confidence", "Best match first"], ["title", "Name A–Z"]]),
    h("button", { type: "button", class: "ghost", onclick: () => { Object.assign(F, { q: "", category: "", brand: "", competitor: "", action: "", min_price: "", max_price: "", min_similarity: 0, in_stock: "", sort: "gap_desc", page: 1 }); syncControls(); loadExplorer(); } }, "Reset"));
  ctl["f-min_similarity"].addEventListener("change", e => { F.min_similarity = parseInt(e.target.value, 10) || 0; });
  return h("div", { class: "card", id: "explorer" }, h("h2", {}, "Product-by-product recommendations"),
    h("p", { class: "note" }, "Each of your products with the competitor products we matched to it, the market average, and a suggested price. Competitor prices are scaled to your pack size when sizes differ."),
    bar, h("div", { id: "ex-count", class: "note" }), h("div", { id: "ex-list" }), h("div", { id: "ex-pager", class: "pager" }));
}
function syncControls() {
  Object.entries({ "f-q": "q", "f-category": "category", "f-brand": "brand", "f-competitor": "competitor", "f-action": "action", "f-in_stock": "in_stock", "f-min_similarity": "min_similarity", "f-min_price": "min_price", "f-max_price": "max_price", "f-sort": "sort" })
    .forEach(([id, k]) => { if (ctl[id]) ctl[id].value = F[k]; });
}
function debounce(fn, ms) { let t; return e => { clearTimeout(t); t = setTimeout(() => fn(e), ms); }; }
function qs() { const u = new URLSearchParams(); Object.entries(F).forEach(([k, v]) => { if (v !== "" && v != null && v !== 0) u.set(k, v); }); return u.toString(); }

let exSeq = 0;
async function loadExplorer() {
  const list = $("#ex-list"); if (!list) return;
  const my = ++exSeq;
  list.style.opacity = .5;
  try {
    const d = await (await fetch(`/api/analyses/${currentId}/pricing?${qs()}`)).json();
    if (my !== exSeq) return;
    if (!facets) { facets = d.facets; const b = ctl["f-brand"]; if (b) facets.brands.forEach(x => b.append(h("option", { value: x.name }, `${x.name} (${x.count})`))); }
    list.innerHTML = ""; list.style.opacity = 1;
    $("#ex-count").textContent = `Showing ${d.total ? (d.page - 1) * d.page_size + 1 : 0}–${Math.min(d.page * d.page_size, d.total)} of ${d.total.toLocaleString()} matched products` + (d.total !== d.all ? ` (filtered from ${d.all.toLocaleString()})` : "");
    if (!d.items.length) list.append(h("p", { class: "note" }, "No products match these filters."));
    d.items.forEach(it => list.append(productCard(it)));
    const pages = Math.max(1, Math.ceil(d.total / d.page_size)), pg = $("#ex-pager"); pg.innerHTML = "";
    const go = n => h("button", { type: "button", class: "ghost" + (n === d.page ? " cur" : ""), disabled: n === d.page, onclick: () => { F.page = n; loadExplorer(); $("#explorer").scrollIntoView({ behavior: "smooth" }); } }, n);
    const nums = [...new Set([1, 2, d.page - 1, d.page, d.page + 1, pages - 1, pages])].filter(n => n >= 1 && n <= pages).sort((a, b) => a - b);
    nums.forEach((n, i) => { if (i && n - nums[i - 1] > 1) pg.append(h("span", {}, "…")); pg.append(go(n)); });
  } catch (e) { list.style.opacity = 1; list.innerHTML = ""; list.append(h("p", { class: "error" }, "Could not load products.")); }
}

const stockChip = (inStock, low) => (inStock === false ? h("span", { class: "chip failed" }, "Out of stock")
  : low ? h("span", { class: "chip warning" }, "Low stock") : inStock === true ? h("span", { class: "chip done" }, "In stock") : h("span", { class: "chip waiting" }, "Stock unknown"));
const unitText = (v, label) => (v == null ? null : `${money(v)} per ${label}`);

function productCard(it) {
  const confCls = it.confidence === "high" ? "done" : it.confidence === "medium" ? "info" : "warning";
  // ---- your product (left)
  const you = h("div", { class: "pc-you" },
    h("div", { class: "pc-label" }, "Your product"),
    h("div", { class: "pc-head" }, thumb(it),
      h("div", { class: "grow" }, h("h3", {}, safeUrl(it.url) ? h("a", { href: it.url, target: "_blank", rel: "noopener noreferrer" }, it.title) : it.title),
        h("div", { class: "pc-meta" }, [it.category, it.brand, it.size, it.sku && "SKU " + it.sku].filter(Boolean).map(x => h("span", { class: "chip waiting" }, x))))),
    h("div", { class: "pc-price" }, h("b", {}, money(it.price)),
      it.compare_at && it.compare_at > it.price ? h("s", {}, money(it.compare_at)) : null,
      it.discount_pct ? h("span", { class: "chip done" }, `${it.discount_pct}% off`) : null),
    h("div", { class: "pc-facts" }, stockChip(it.in_stock, it.low_stock),
      unitText(it.unit_price, it.unit_label) ? h("small", {}, unitText(it.unit_price, it.unit_label)) : null,
      h("small", {}, (() => {
        const live = it.competitors.filter(e => !e.ignored), cheaper = live.filter(e => e.adjusted_price > it.price).length;
        return cheaper === live.length ? "Cheapest of all matched products" : cheaper === 0 ? "Most expensive of all matched products" : `Cheaper than ${cheaper} of ${live.length} matched competitors`;
      })())));

  // ---- competitor products (right): always visible
  const rows = it.competitors.map(e => {
    const diff = it.price ? (e.adjusted_price - it.price) / it.price * 100 : null;
    return h("tr", { class: e.ignored ? "ignored" : "" },
      h("td", { class: "ct-thumb" }, safeUrl(e.image) ? h("img", { class: "thumb sm", src: e.image, alt: "", loading: "lazy", referrerpolicy: "no-referrer" }) : null),
      h("td", { style: "white-space:normal" }, h("b", {}, e.competitor_name),
        h("div", { class: "ct-title" }, safeUrl(e.url) ? h("a", { href: e.url, target: "_blank", rel: "noopener noreferrer" }, e.title) : e.title),
        h("small", {}, [e.size, `match ${Math.round(e.confidence * 100)}%`, e.size_adjusted ? "scaled to your pack size" : null].filter(Boolean).join(" · ")),
        e.ignored ? h("small", { class: "ignnote" }, `Ignored: ${e.ratio}× different from your price (probably a different pack size or product)`) : null),
      h("td", { class: "num" }, h("b", {}, money(e.adjusted_price)),
        e.size_adjusted ? h("small", { style: "display:block" }, `listed ${money(e.price)}`) : null,
        e.compare_at && e.compare_at > e.price ? h("small", { style: "display:block" }, h("s", {}, money(e.compare_at)), ` ${e.discount_pct}% off`) : null,
        unitText(e.unit_price, e.unit_label) ? h("small", { style: "display:block" }, unitText(e.unit_price, e.unit_label)) : null),
      h("td", { class: "num " + (e.ignored || diff == null ? "" : diff < -3 ? "cheap" : diff > 3 ? "dear" : "") }, e.ignored || diff == null ? "–" : pct(diff)),
      h("td", {}, stockChip(e.in_stock, e.low_stock)));
  });
  const comp = h("div", { class: "pc-comp" },
    h("div", { class: "pc-label" }, `Competitor products (${it.n_matches} matched${it.n_ignored ? `, ${it.n_ignored} ignored` : ""})`),
    h("div", { class: "tablewrap" }, h("table", { class: "ctable" },
      h("thead", {}, h("tr", {}, ["", "Competitor product", "Price", "vs you", "Stock"].map((x, i) => h("th", { class: i === 2 || i === 3 ? "num" : "" }, x)))), h("tbody", {}, rows))),
    h("div", { class: "pc-market" }, h("span", {}, "Market average ", h("b", {}, money(it.market_avg))), h("span", {}, "Lowest ", h("b", {}, money(it.market_min))), h("span", {}, "Highest ", h("b", {}, money(it.market_max))),
      h("span", { class: it.gap_pct > 0 ? "neg" : it.gap_pct < 0 ? "pos" : "" }, "You are ", h("b", {}, pct(it.gap_pct)), " vs average")),
    h("small", { class: "why" }, "Why matched: ", [...new Set(it.competitors.filter(e => !e.ignored).flatMap(e => e.reasons))].slice(0, 5).join("; ")));

  const reco = h("div", { class: "reco " + it.action },
    h("div", {}, h("span", { class: "chip " + ACTION_CLASS[it.action] }, ACTION_LABEL[it.action]), " ",
      it.action !== "hold" ? h("b", {}, `${money(it.price)} → ${money(it.suggested_price)} (${pct(it.change_pct)})`) : null,
      h("span", { class: "chip " + confCls, style: "float:right", title: "Based on number of competitors, match quality and price spread" }, it.confidence + " confidence")),
    h("p", {}, it.rationale),
    it.flags.length ? h("ul", { class: "flags" }, it.flags.map(f => h("li", {}, f))) : null);
  return h("article", { class: "pcard2" }, you, comp, reco);
}

// Matches we refused to use because the prices were implausibly far apart (usually different pack sizes)
function suspectCard(r) {
  const list = ((r.pricing || {}).suspect) || [];
  if (!list.length) return null;
  const body = h("tbody", {}, list.slice(0, 60).map(x => h("tr", {},
    h("td", { style: "white-space:normal" }, safeUrl(x.url) ? h("a", { href: x.url, target: "_blank", rel: "noopener noreferrer" }, x.title) : x.title, h("small", { style: "display:block" }, x.size || "size not stated")),
    h("td", { class: "num" }, money(x.price)),
    h("td", { style: "white-space:normal" }, h("b", {}, x.competitor_name), h("div", { class: "ct-title" }, safeUrl(x.comp_url) ? h("a", { href: x.comp_url, target: "_blank", rel: "noopener noreferrer" }, x.comp_title) : x.comp_title), h("small", {}, x.comp_size || "size not stated")),
    h("td", { class: "num" }, money(x.comp_adjusted)), h("td", { class: "num" }, h("span", { class: "chip warning" }, x.ratio + "×")))));
  return h("div", { class: "card" }, h("h2", {}, `Matches ignored because prices were far apart (${list.length})`),
    h("p", { class: "note" }, "These looked like the same product by name, but the prices differ by more than 3×, which almost always means a different pack size (single can vs 24-pack) or a different product. They are NOT used in the averages or recommendations. Open the links to check."),
    h("div", { class: "tablewrap" }, h("table", {}, h("thead", {}, h("tr", {}, ["Your product", "Your price", "Competitor product", "Their price", "Gap"].map((x, i) => h("th", { class: i === 1 || i === 3 || i === 4 ? "num" : "" }, x)))), body)));
}

// Where the time went (handy when a hosted run feels slow)
function timingsCard(r) {
  const t = r.timings;
  if (!t || !Object.keys(t).length) return null;
  const rows = Object.entries(t).filter(([k]) => k !== "Total").map(([k, v]) => h("tr", {}, h("td", {}, k), h("td", { class: "num" }, v < 1 ? "< 1 s" : v + " s")));
  return h("div", { class: "card" }, h("h2", {}, `Where the time went (${t.Total} s in total)`),
    h("div", { class: "tablewrap" }, h("table", {}, h("thead", {}, h("tr", {}, h("th", {}, "Step"), h("th", { class: "num" }, "Time"))), h("tbody", {}, rows))));
}
