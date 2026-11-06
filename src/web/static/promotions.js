"use strict";
// "Competitor promotions" tab: how deep, on what, and for how long competitors discount, read from their websites
// (product "was" prices + banner / announcement text). Uses helpers from app.js (h, $, money, safeUrl, chart, chartCard, kpi, OKABE).

const pp = (n, d = 1) => (n == null ? "–" : Number(n).toFixed(Number.isInteger(n) ? 0 : d) + "%");
const niceDate = iso => (iso ? new Date(iso + "T00:00:00").toLocaleDateString(undefined, { day: "numeric", month: "short" }) : "");
const STATUS_CHIP = { active: "done", upcoming: "info", ongoing: "waiting", expired: "failed" };
const STATUS_TEXT = { active: "Running now", upcoming: "Upcoming", ongoing: "No end date shown", expired: "Ended" };

function periodText(c) {
  if (c.start && c.end) return `${niceDate(c.start)} → ${niceDate(c.end)}` + (c.status === "active" && c.days_left != null ? ` (${c.days_left} day${c.days_left === 1 ? "" : "s"} left)` : "");
  if (c.end) return `Until ${niceDate(c.end)}` + (c.status === "active" && c.days_left != null ? ` (${c.days_left} day${c.days_left === 1 ? "" : "s"} left)` : "");
  if (c.start) return `From ${niceDate(c.start)}`;
  if (c.duration_days) return `${c.duration_days}-day offer`;
  return c.event ? c.event : "No dates shown";
}

function promotionsTab(r) {
  const P = r.promotions || {}, box = h("div");
  const sites = P.sites || {}, S = P.summary || {}, src = P.source_key || r.source_key;
  const order = [src, ...(r.competitors || []).map(c => c.key).filter(k => k !== src)].filter(k => sites[k]);
  const nameOf = k => (sites[k] || {}).name || k;
  if (!order.length) { box.append(h("div", { class: "card" }, h("p", { class: "note" }, "No promotion data is available for this analysis."))); return box; }

  box.append(h("div", { class: "note", style: "margin:0 0 12px" },
    "Read from each website's own pages: crossed-out “was” prices on products (amount and product type) and banner / announcement text (offer, dates, codes). Social-media posts and ads are not included yet."));

  box.append(h("div", { class: "kpis" },
    kpi(`${S.competitors_with_sales || 0} of ${S.competitors || 0}`, "competitors have products on sale"),
    kpi(S.deepest ? pp(S.deepest.avg_discount_pct) : "–", S.deepest ? `deepest average discount (${S.deepest.name})` : "deepest average discount"),
    kpi(S.broadest ? pp(S.broadest.on_sale_pct) : "–", S.broadest ? `of catalogue on sale at ${S.broadest.name}` : "broadest sale"),
    kpi(S.active_campaigns || 0, "competitor campaigns live or upcoming"),
    kpi(S.your_on_sale_pct == null ? "–" : pp(S.your_on_sale_pct), `your catalogue on sale${S.your_avg_discount_pct != null ? ` (avg ${pp(S.your_avg_discount_pct)} off)` : ""}`)));

  // ---- insights
  if ((P.insights || []).length)
    box.append(h("div", { class: "card" }, h("h2", {}, "What stands out"),
      h("ul", { class: "insights" }, P.insights.map(i => h("li", {}, h("span", { class: "chip " + i.severity }, i.title), " ", i.text)))));

  // ---- discount depth by product type (matrix)
  const types = P.types || [];
  if (types.length) {
    const head = h("tr", {}, h("th", {}, "Product type"), ...order.map(k => h("th", { class: "num" }, nameOf(k) + (k === src ? " (you)" : ""))), h("th", {}, "Gap vs best competitor"));
    const body = h("tbody");
    types.slice(0, 25).forEach(row => {
      const tds = order.map(k => {
        const c = (row.sites || {})[k];
        if (!c || !c.products) return h("td", { class: "num", style: "color:var(--dim)" }, "–");
        if (!c.on_sale) return h("td", { class: "num", style: "color:var(--dim)", title: `${c.products} products, none on sale` }, "no sale");
        const a = Math.min((c.avg_discount_pct || 0) / 40, 1) * 0.55;
        return h("td", { class: "num", style: `background:rgba(245,158,11,${a.toFixed(2)})`, title: `${c.on_sale} of ${c.products} products on sale, up to ${pp(c.max_discount_pct)} off` },
          h("b", {}, pp(c.avg_discount_pct)), h("small", { style: "display:block;color:var(--muted)" }, `${c.on_sale}/${c.products} items`));
      });
      const gap = row.gap_pts;
      tds.push(h("td", {}, gap == null ? "–" : h("span", { class: "chip " + (gap >= 5 ? "warning" : gap <= -5 ? "done" : "info") },
        gap >= 5 ? `Competitors deeper by ${gap.toFixed(1)} pts` : gap <= -5 ? `You are deeper by ${Math.abs(gap).toFixed(1)} pts` : "About level")));
      body.append(h("tr", {}, h("td", {}, h("b", {}, row.type)), ...tds));
    });
    box.append(h("div", { class: "card" }, h("h2", {}, "Discount depth by product type"),
      h("p", { class: "note" }, "Average % off among products currently on sale, and how many items that is. Darker = deeper discount. Hover a cell for details."),
      h("div", { class: "tablewrap" }, h("table", {}, h("thead", {}, head), body))));
  }

  // ---- charts
  const g = h("div", { class: "grid2" });
  const c1 = chartCard("Average discount by product type", "Only product types where at least one competitor has items on sale.");
  const c2 = chartCard("Share of catalogue on sale", "And the average depth of those discounts.");
  const c3 = chartCard("How deep the discounts are", "Number of discounted products in each depth band.");
  g.append(c1.el, c2.el, c3.el); box.append(g);
  const topTypes = types.filter(t => t.competitors_on_sale > 0).slice(0, 8);
  chart(c1.canvas, { type: "bar", data: { labels: topTypes.map(t => t.type), datasets: order.map((k, i) => ({
      label: nameOf(k) + (k === src ? " (you)" : ""), data: topTypes.map(t => ((t.sites || {})[k] || {}).avg_discount_pct || 0), backgroundColor: k === src ? SOURCE_COLOR : OKABE[(i - 1 + OKABE.length) % OKABE.length] })) },
    options: { maintainAspectRatio: false, scales: { y: { title: { display: true, text: "% off (average)" } } } } });
  chart(c2.canvas, { type: "bar", data: { labels: order.map(nameOf), datasets: [
      { label: "% of products on sale", data: order.map(k => sites[k].on_sale_pct || 0), backgroundColor: order.map((k, i) => (k === src ? SOURCE_COLOR : OKABE[(i - 1 + OKABE.length) % OKABE.length])) },
      { label: "Average % off", data: order.map(k => sites[k].avg_discount_pct || 0), backgroundColor: "#7a604a" }] }, options: { maintainAspectRatio: false } });
  const bands = (sites[order[0]].bands || []).map(b => b.label);
  chart(c3.canvas, { type: "bar", data: { labels: order.map(nameOf), datasets: bands.map((lab, bi) => ({ label: lab, data: order.map(k => ((sites[k].bands || [])[bi] || {}).count || 0),
      backgroundColor: ["#fde68a", "#fbbf24", "#f59e0b", "#d97706", "#92400e"][bi] })) }, options: { maintainAspectRatio: false, scales: { x: { stacked: true }, y: { stacked: true } } } });

  // ---- campaigns / banner offers
  const camps = P.campaigns || [];
  const cbody = h("tbody");
  camps.forEach(c => cbody.append(h("tr", {},
    h("td", {}, h("b", {}, c.site_name), c.is_source ? h("span", { class: "chip source", style: "margin-left:6px" }, "you") : null),
    h("td", { style: "white-space:normal" }, h("b", {}, c.summary), c.up_to ? "" : "", h("small", { style: "display:block;color:var(--muted);white-space:normal" }, `“${c.text.length > 140 ? c.text.slice(0, 140) + "…" : c.text}”`)),
    h("td", {}, c.applies_to || h("span", { style: "color:var(--dim)" }, "not stated")),
    h("td", { style: "white-space:normal" }, h("span", { class: "chip " + STATUS_CHIP[c.status] }, STATUS_TEXT[c.status]), h("small", { style: "display:block;color:var(--muted)" }, periodText(c)),
      c.seen_days != null && c.seen_days >= 1 ? h("small", { style: "display:block;color:var(--dim)" }, `seen on our checks for ${Math.round(c.seen_days)} day${Math.round(c.seen_days) === 1 ? "" : "s"}`) : null),
    h("td", {}, c.code ? h("code", {}, c.code) : "–"),
    h("td", { style: "white-space:normal" }, [c.min_spend ? `min spend ${money(c.min_spend)}` : null, ...(c.conditions || [])].filter(Boolean).join(", ") || "–"))));
  box.append(h("div", { class: "card" }, h("h2", {}, "Campaigns and banner offers"),
    h("p", { class: "note" }, "Offers announced on each homepage, broken into amount, product type, conditions and dates. Dates are only shown when the site states them (or says “today only”, “this weekend”…)."),
    camps.length ? h("div", { class: "tablewrap" }, h("table", {}, h("thead", {}, h("tr", {}, ["Competitor", "Offer", "Applies to", "Period", "Code", "Conditions"].map(x => h("th", {}, x)))), cbody))
      : h("p", { class: "note" }, "No banner offers were found on the homepages. Sites that load banners with JavaScript can’t be read this way.")));

  // ---- how long discounts run (needs earlier analyses)
  const H = P.history || {}, hk = order.filter(k => H[k]);
  const hist = h("div", { class: "card" }, h("h2", {}, "How long discounts last"));
  if (hk.length) {
    const rows = hk.map(k => { const x = H[k]; return h("tr", {}, h("td", {}, h("b", {}, nameOf(k))), h("td", { class: "num" }, x.runs_observed),
      h("td", { class: "num" }, x.median_days_on_sale < 1 ? "< 1 day" : x.median_days_on_sale + " days"), h("td", { class: "num" }, x.longest_days_on_sale < 1 ? "< 1 day" : x.longest_days_on_sale + " days"),
      h("td", { class: "num" }, x.new_discounts_this_run), h("td", { class: "num" }, x.discounts_ended_since_last_run), h("td", {}, niceDate(x.observed_since))); });
    hist.append(h("p", { class: "note" }, "Measured from our repeated checks of each site: how long today’s discounted products have stayed discounted. The real start can be earlier than our first check."),
      h("div", { class: "tablewrap" }, h("table", {}, h("thead", {}, h("tr", {}, ["Site", "Checks", "Typical duration", "Longest", "New this check", "Ended since last", "Tracked since"].map(x => h("th", {}, x)))), h("tbody", {}, rows))));
  } else hist.append(h("p", { class: "note" }, "Not measurable yet. Run the analysis again later, or set up a monitor in “History & offers”, and we will track how long each discount stays in place."));
  box.append(hist);

  // ---- top deals (per site)
  const withDeals = order.filter(k => (sites[k].top_deals || []).length);
  if (withDeals.length) {
    const sel = h("select", { "aria-label": "Site", onchange: e => draw(e.target.value) }, withDeals.map(k => h("option", { value: k }, nameOf(k) + (k === src ? " (you)" : ""))));
    sel.value = withDeals.find(k => k !== src) || withDeals[0];
    const tbl = h("div", { class: "tablewrap" });
    const draw = k => {
      tbl.innerHTML = "";
      tbl.append(h("table", {}, h("thead", {}, h("tr", {}, ["Product", "Type", "Was", "Now", "Off"].map((x, i) => h("th", { class: i > 1 ? "num" : "" }, x)))),
        h("tbody", {}, sites[k].top_deals.map(d => h("tr", {}, h("td", { style: "white-space:normal" }, safeUrl(d.url) ? h("a", { href: d.url, target: "_blank", rel: "noopener noreferrer" }, d.title) : d.title),
          h("td", {}, d.type), h("td", { class: "num", style: "text-decoration:line-through;color:var(--dim)" }, money(d.was)), h("td", { class: "num" }, money(d.price)), h("td", { class: "num" }, h("b", {}, pp(d.pct))))))));
    };
    draw(sel.value);
    box.append(h("div", { class: "card" }, h("div", { class: "toolbar", style: "justify-content:space-between" }, h("h2", { style: "margin:0" }, "Biggest discounts right now"), sel), tbl));
  }
  box.append(h("div", { class: "toolbar", style: "justify-content:flex-end" }, h("a", { class: "btn ghost-btn", href: `/api/analyses/${currentId}/promotions.csv` }, "Export promotions CSV")));
  return box;
}
