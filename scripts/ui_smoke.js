// UI end-to-end smoke test (needs Node + `npm i jsdom`). Start `python -m src.web --demo` first, then: node scripts/ui_smoke.js
const { JSDOM } = require("jsdom");
const BASE = process.env.BASE || "http://localhost:8000";
const sleep = ms => new Promise(r => setTimeout(r, ms));
let failures = 0;
const check = (name, ok, extra = "") => { console.log((ok ? "PASS " : "FAIL ") + name + (extra ? "  " + extra : "")); if (!ok) failures++; };
(async () => {
  const errors = [], charts = [];
  const html = await (await fetch(BASE + "/")).text();
  const dom = new JSDOM(html.replace(/<script[^>]*><\/script>/g, ""), { url: BASE + "/", runScripts: "outside-only", pretendToBeVisual: true });
  const w = dom.window;
  w.fetch = (u, o) => fetch(new URL(u, BASE), o);
  w.Chart = class { constructor(c, cfg) { charts.push(cfg); } destroy() {} };
  w.confirm = () => true;
  w.HTMLElement.prototype.scrollIntoView = () => {};
  w.addEventListener("error", e => errors.push(e.message));
  // browsers share globals between <script> tags; strict-mode eval does not, so evaluate both files as one
  let code = ""; for (const f of ["app.js", "pricing.js", "promotions.js"]) code += (await (await fetch(`${BASE}/static/${f}`)).text()).replace(/^"use strict";/, "") + "\n";
  w.eval(code);
  await sleep(600);
  const $ = s => w.document.querySelector(s), $$ = s => [...w.document.querySelectorAll(s)];

  $("#demo").click();
  check("demo fills 5 competitors", $$(".comp-input").length === 5);
  $("#form").dispatchEvent(new w.Event("submit", { cancelable: true }));
  for (let i = 0; i < 120; i++) { await sleep(500); if (!$("#results").classList.contains("hidden") && $("#progress-card").classList.contains("hidden")) break; }
  check("results shown", !$("#results").classList.contains("hidden"));
  check("6 tabs", $$(".tab").length === 6);
  await sleep(1500);
  const cards = () => $$("#ex-list .pcard2");
  check("explorer renders product cards", cards().length === 20, `(${cards().length})`);
  check("count line mentions total", /of \d+ matched products/.test($("#ex-count").textContent), $("#ex-count").textContent);
  check("recommendation present on cards", cards().every(c => /Reduce price|Raise price|Keep price/.test(c.textContent)));
  const card0 = $("#ex-list article");
  check("card shows YOUR product panel with price and stock", !!card0.querySelector(".pc-you") && /Your product/.test(card0.textContent) && !!card0.querySelector(".pc-you .pc-price b") && /In stock|Out of stock|Low stock|Stock unknown/.test(card0.querySelector(".pc-you").textContent));
  check("competitor products are visible by default with price + stock", card0.querySelectorAll(".ctable tbody tr").length >= 1 && /Competitor products/.test(card0.textContent) && !!card0.querySelector(".ctable .chip"));
  check("competitor rows link to their product pages", [...card0.querySelectorAll(".ctable a")].every(a => /^https?:/.test(a.href)) && card0.querySelectorAll(".ctable a").length >= 1);
  check("market average line present", /Market average/.test(card0.textContent) && /Lowest/.test(card0.textContent));
  check("risk banner filled", $$("#risk-list .toprow").length > 0, `(${$$("#risk-list .toprow").length})`);
  check("charts drawn", charts.length >= 4);
  const total = parseInt($("#ex-count").textContent.match(/of (\d+)/)[1], 10);
  check("more than 100 products compared (no cap)", total > 100, `(${total})`);
  check("pagination present", $$("#ex-pager button").length >= 2);

  const sel = $("#f-category"); sel.value = [...sel.options].find(o => /Meat/.test(o.value)).value; sel.dispatchEvent(new w.Event("change"));
  await sleep(1200);
  check("category filter narrows list", parseInt($("#ex-count").textContent.match(/of (\d+)/)[1], 10) < total, $("#ex-count").textContent);
  const act = $("#f-action"); act.value = "decrease"; act.dispatchEvent(new w.Event("change"));
  await sleep(1200);
  check("action filter only shows 'Reduce price'", cards().length > 0 && cards().every(c => /Reduce price/.test(c.textContent)));
  check("html in titles stays text", !$$("#ex-list script").length);

  // ---- recent analyses are cards without "(done)" text
  await w.loadRecent();
  check("recent analyses render as cards", $$("#recent .rcard").length >= 1 && /Recent analyses/.test($("#recent").textContent));
  check("recent cards show no '(done)'/'(failed)' text", !/\((?:done|failed|running|queued)\)/.test($("#recent").textContent) && /vs \d+ competitor/.test($("#recent").textContent));
  check("recent cards expose status without visible text", [...$$("#recent .rcard")].every(c => /Completed|Failed|Running|Queued/.test(c.getAttribute("aria-label"))));

  // ---- competitor promotions tab
  $$(".tab").find(t => /Competitor promotions/.test(t.textContent)).click();
  await sleep(300);
  const txt = $("#results").textContent;
  check("promotions: KPIs and notes render", /competitors have products on sale/.test(txt) && /Social-media posts and ads are not included/.test(txt));
  check("promotions: discount-by-type matrix", /Discount depth by product type/.test(txt) && $$("#results table").length >= 3);
  check("promotions: meat row shows a competitor gap", /Meat & Poultry/.test(txt) && /Competitors deeper by/.test(txt));
  check("promotions: campaigns table lists offers with codes", /MEAT22/.test(txt) && /Running now|Upcoming/.test(txt));
  check("promotions: charts drawn", charts.length >= 3);
  check("promotions: top deals table", /Biggest discounts right now/.test(txt) && $$("#results a[target=_blank]").length > 0);
  check("promotions: CSV export link", !!$$("#results a").find(a => /promotions\.csv/.test(a.getAttribute("href") || "")));

  $$(".tab").find(t => /^Competitors$/.test(t.textContent.trim())).click();
  check("competitors tab renders", /Price index/.test($("#results").textContent));
  $$(".tab").find(t => /Data quality/.test(t.textContent)).click();
  check("data quality shows where the time went", /Where the time went/.test($("#results").textContent));
  check("data quality lists 6 sites", $$("#results tbody tr").length >= 6);
  check("no script errors", errors.length === 0, errors.join("; "));
  console.log(failures ? `\n${failures} FAILED` : "\nall UI checks passed");
  process.exit(failures ? 1 : 0);
})().catch(e => { console.error(e); process.exit(2); });
