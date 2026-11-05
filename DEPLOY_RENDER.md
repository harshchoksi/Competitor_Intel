# Deploying on Render: what goes wrong and how it is fixed

## Why it was fast on your PC and "running for 20 minutes" on Render

| # | Cause | Local | Render | Fixed by |
|---|-------|-------|--------|----------|
| 1 | **Fast feed blocked, so the slow page-by-page crawl runs.** Shopify (`/products.json`) and WooCommerce (Store API) are read in a handful of requests. If the site refuses the request (rate limit / bot protection against cloud IP addresses) the app silently falls back to opening every product page, at 2 pages per second per site. Your real run had ~7,700 products: a 1,850-product store alone is **15+ minutes**. | feed works from a home IP | often refused from a datacenter IP | per-site time budget (10 min hosted), `Retry-After` honoured, a visible warning on the site's card, see "How to confirm" |
| 2 | **Crawler also downloaded product images.** Shopify sitemaps list every image next to its product; the old check missed `.jpg?v=123`, so ~35% of crawl requests were images (your stuck run cached 719 images, 254 MB). | rarely reached | whenever #1 happens | sitemap parser only reads page URLs |
| 3 | **Out of memory, then "running" forever.** Starting the app cost 228 MB (PDF libraries loaded at boot). A 512 MB instance plus a 7,700-product analysis gets killed; the analysis thread dies with it, but the database still says "running". | 16 GB RAM | 512 MB on small plans | PDF libs load only when you download a PDF (idle RAM 228 -> 94 MB); one analysis at a time on hosted; on restart, interrupted analyses are marked failed with a clear message |
| 4 | **Weak CPU.** Matching ~8k products took ~17-20 s on a full core. The free plan has ~0.1 CPU: 10x slower. | fast CPU | shared 0.1-0.5 CPU | use a plan with >= 1 CPU for real catalogues |
| 5 | **Database round trips.** Saving 2,000 products took **4,005** SQL statements, each a network trip to Render Postgres. | SQLite, no network | Postgres over network | batched inserts: **9** statements |
| 6 | **AI step.** Gemini calls had a 90 s timeout x 3 retries per call, repeated for every competitor, so an unreachable/rate-limited API could add many minutes. | usually fast | free-tier 429s / slow | 30 s timeout, stops after the first failure, 60 s total budget |
| 7 | **Free web services sleep** after 15 min without visits. A closed tab during a long analysis = server sleeps = job lost. | n/a | yes | paid plan, or keep the tab open |

## The database error ("duplicate key / already exists")

You saw one of these; each is reproduced in `tests/test_deploy_hardening.py` and fixed:

* `duplicate key value violates unique constraint "sites_key_key"` (or `UNIQUE constraint failed: sites.key`): **two analyses saving the same website at once**, e.g. you clicked Analyze again because the first looked stuck. Both found "site not in the database yet" and both inserted it. Fixed with a write lock (in-process) plus a Postgres advisory lock (across processes), and one retry.
* `price_snapshots_product_id_run_id_key` / `products_site_id_external_id_key`: the same product id twice in one scrape. Fixed: duplicates are collapsed before saving.
* `value too long for type character varying(300)`: **Postgres enforces column lengths; SQLite ignores them**, so it never failed locally. Fixed: text is clipped to the column size.
* `duplicate key ... pg_type_typname_nsp_index`: two server processes creating tables at the same moment (overlapping deploy, or more than one worker). Fixed with an advisory lock in `init_db`.
* `duplicate key ... <table>_pkey Key (id)=(N) already exists`: id counters behind the data, which happens after copying a SQLite file into Postgres. Fixed: counters are repaired at startup.
* Also: `postgres://` / `postgresql://` URLs (what Render gives you) are converted for the installed driver; connections use `pool_pre_ping`.

## How to confirm which cause hit you (Render dashboard > Logs)
* Lines like `shop.example.com: 1850 products via jsonld` for a **Shopify or WooCommerce** store mean cause #1. The healthy lines say `via shopify_products_json` or `via woocommerce_store_api`. A line `public feed not available` or `Giving up on ... HTTP 429/403` names the refused request.
* `Out of memory` / `Instance failed ... exceeded memory` in Events = cause #3.
* `Starting ...` appearing again in the middle of a run = a restart (OOM, deploy, or sleep).
* The exact database error text = match it against the list above.

## Settings (render.yaml has them)
| Variable | Value | Why |
|----------|-------|-----|
| `DATABASE_URL` | Render Postgres connection string | history survives deploys (the container disk is wiped on every deploy; SQLite there loses everything) |
| `WEB_CONCURRENCY` | `1` | analyses live in one process |
| `ANALYSIS_WORKERS` | `1` (hosted default) | one analysis at a time keeps memory bounded |
| `SITE_TIME_BUDGET_MINUTES` | `10` (hosted default) | partial results beat waiting an hour |
| `GEMINI_TIMEOUT_SECONDS` | `30` | AI can never stall a run |
| `SEMANTIC_EMBEDDINGS` | `off` | the optional model needs ~1 GB RAM |

Plan guidance: **Starter (512 MB)** handles small catalogues; for ~8,000 products across 6 sites use **Standard (2 GB, 1 CPU)**. The Free plan (512 MB, 0.1 CPU, sleeps) is not suitable.

## If a competitor is always slow
Check its card in "Data quality": a note saying the fast feed was unreachable means the site is refusing this server. Options: run again later, lower *Requests / second*, or use *Max products*/*product types* to read only what you need.

## When a competitor site cannot be read
Each failed site now says WHY, in plain words, in its progress line and in "Data quality". What the app tries, in order:
1. **The address is the company's main site, not its shop** (e.g. `lingandsons.com` vs `shop.lingandsons.com`): it follows "Shop / Order online" links on the homepage, then tries `shop.`, `store.`, `order.` sub-domains, and reads the store it finds. The result says "read the online store at ...".
2. **No sitemap**: it follows the site's category pages (and their "page 2, 3..." links) to find product pages.
3. **Blocked (HTTP 403 / bot check) from this server**: if you set `SCRAPER_PROXY_URL`, ONLY the blocked requests are retried through that proxy. Without it the site is reported as blocked, with the advice to run the tool from your own computer or through a proxy.
4. **Built with JavaScript, or robots.txt forbids it**: reported clearly. Browser support is optional (`requirements-browser.txt`, needs >= 1 GB RAM). A site's robots.txt is always respected.

A proxy is a paid third-party service (residential/rotating proxies, or a "web unlocker" API that exposes a proxy address). It can route around blocks that some sites put up on purpose; check the site's terms before using one. For sites you are allowed to read, entering the shop address directly is usually enough.

## Why a run takes the time it takes
Open **Data quality > Where the time went** after a run: it lists reading/saving the sites, matching and pricing (including the AI check of borderline matches), history, and the AI summary. Typical levers: product feeds are read 3 pages at a time at up to 5 requests/second (page-by-page crawling stays at your 2/s setting); matching 8,000 products takes about 1.5 s.
