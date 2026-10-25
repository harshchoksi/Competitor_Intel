const { JSDOM } = require("jsdom");
const BASE = "http://localhost:8000";
const sleep = ms => new Promise(r => setTimeout(r, ms));
(async () => {
  const errors = [], charts = [];
  const html = await (await fetch(BASE + "/")).text();
  const dom = new JSDOM(html.replace(/<script src="https:[^>]*><\/script>/, "").replace('<script src="/static/app.js"></script>', ""), { url: BASE + "/", runScripts: "outside-only", pretendToBeVisual: true });
  const w = dom.window;
  let injectAI = false;
  w.fetch = async (u, o) => {
    const r = await fetch(new URL(u, BASE), o);
    if (injectAI && /\/api\/analyses\/[a-f0-9]+$/.test(String(u)) && (!o || !o.method || o.method === "GET")) {
      const d = await r.clone().json();
      if (d.status === "done" && d.result) {
        d.result.ai = { status: "ok", model: "gemini-test", executive_summary: "Summary text <b>not bold</b>", removed_statements: 2, key_findings: [{ title: "T", text: "x", severity: "warning" }],
          recommended_actions: [{ priority: "high", action: "Do it", rationale: "Because" }], risks: ["r1"], per_competitor_notes: { "localhost:8102": "note" } };
        return new Response(JSON.stringify(d), { status: 200, headers: { "content-type": "application/json" } });
      }
    }
    return r;
  };
  w.confirm = () => true;
  w.Chart = class { constructor(c, cfg) { this.cfg = cfg; charts.push(cfg); } destroy() {} };
  w.addEventListener("error", e => errors.push(e.message));
  w.eval(await (await fetch(BASE + "/static/app.js")).text());
  await sleep(600);
  const $ = s => w.document.querySelector(s), $$ = s => [...w.document.querySelectorAll(s)];
  const titles = () => $$("#results h2").map(e => e.textContent);
  const analyze = async () => {
    $("#results").classList.add("hidden");
    $("#form").dispatchEvent(new w.Event("submit", { cancelable: true }));
    for (let i = 0; i < 90; i++) { await sleep(500); if (!$("#results").classList.contains("hidden") && $("#progress-card").classList.contains("hidden")) break; }
  };
  console.log("health -> demo btn:", !$("#bump").classList.contains("hidden"), "| AI checkbox disabled (no key):", $("#use-ai").disabled, "|", $("#ai-hint").textContent);
  $("#demo").click();
  await analyze();
  console.log("1st run sections:", titles().join(" | "));
  console.log("  changes card says first analysis:", $$("#results .card").some(c => /first analysis/.test(c.textContent)));
  console.log("  offers table rows:", $$("#results table").find(t => /Free shipping/.test(t.textContent)).querySelectorAll("tbody tr").length,
    "| coupon tags:", $$("#results .tag").filter(t => /^SAVE/.test(t.textContent)).length, "| ad-pixel tags:", $$("#results .tag.ad").length);
  const mk = $$("#results table").find(t => /Public ad libraries/.test(t.textContent));
  console.log("  marketing rows:", mk.querySelectorAll("tbody tr").length, "| ad library links:", [...mk.querySelectorAll("a")].filter(a => /ads\/library|adstransparency/.test(a.href)).length);
  console.log("  monitor card present:", titles().includes("Monitor this comparison"));

  await $("#bump").click(); await sleep(600);
  injectAI = true;
  await analyze();
  injectAI = false;
  console.log("2nd run sections:", titles().join(" | "));
  const ch = $$("#results table").find(t => /Went out of stock/.test(t.textContent));
  console.log("  changes table:", [...ch.querySelectorAll("tbody tr")].map(r => r.textContent.replace(/\s+/g, " ")).slice(0, 3));
  console.log("  biggest-drops rows:", $$("#results h3").find(h => h.textContent === "Biggest price drops") ? "yes" : "no", "| history line chart:", charts.some(c => c.type === "line"), "| product selector options:", $$("#results select option").length);
  console.log("  price-drop alerts:", $$("#results .alert").filter(a => /cut the price/.test(a.textContent)).length);

  console.log("AI card rendered:", !!$(".aibox"), "| html injected via AI text:", $$(".aibox b").length === 0 ? "no (escaped)" : "YES (bad)", "| mentions removed count:", /2 statement/.test($(".aibox").textContent));
  const aid = w.location.hash.match(/#a=([a-f0-9]+)/)[1];

  // monitors
  const mc = $$("#results .card").find(c => /Monitor this comparison/.test(c.textContent));
  mc.querySelector("button.primary").click(); await sleep(8000);
  console.log("monitors card visible:", !$("#monitors-card").classList.contains("hidden"), "| entries:", $$("#monitors .mon").length, "|", $("#monitors .mon") ? $("#monitors .mon").textContent.replace(/\s+/g, " ").slice(0, 160) : "");
  const mons = await (await fetch(BASE + "/api/monitors")).json();
  console.log("monitor API:", mons.length, "monitor(s); first run analyses:", mons[0].analyses.map(x => x.status).join(","));
  // PDF with all new sections
  const pdf = Buffer.from(await (await fetch(BASE + `/api/analyses/${aid}/report.pdf`)).arrayBuffer());
  require("fs").writeFileSync("/tmp/rep/full.pdf", pdf);
  console.log("PDF:", pdf.length, "bytes", pdf.subarray(0, 5).toString());
  console.log("JS errors:", errors.length ? errors : "none");
  process.exit(0);
})();
