# Competitor Price Intelligence (website prototype)

Type your store plus up to 5 competitor URLs, click **Analyze**, get a dashboard and a PDF report: price comparison,
discounts, stock, offers/shipping/returns, ad-tag and social signals, changes since last time, AI summary, alerts.
There is **no login**: run it on your own computer or behind a firewall.

## Quick start (Python 3.11+)
```bash
python -m venv .venv && source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python -m src.web --demo                                    # open http://localhost:8000
```
In the page: **Fill demo URLs** > **Analyze** > **Demo: change competitor prices** > **Analyze** again to see changes and alerts.
Startup takes a few seconds (PDF libraries load). For real sites run `python -m src.web` and type real URLs.

## Optional settings (copy `.env.example` to `.env`)
| Feature | Setting | Cost |
|---|---|---|
| AI summary (Gemini) | `GEMINI_API_KEY` (Google AI Studio) | Free tier; check Google's terms on how free-tier data may be used |
| YouTube stats | `YOUTUBE_API_KEY` | Free quota |
| Email alerts | `SMTP_HOST/PORT/USER/PASSWORD/FROM` | Your mail provider |
| Telegram alerts | `TELEGRAM_BOT_TOKEN`, then chat id in the monitor form | Free |
| JavaScript-rendered sites | `pip install -r requirements-browser.txt && playwright install chromium` | Free, ~400 MB |

Without keys everything else still works; the AI card is simply hidden and rule-based insights are shown.

## What is in it
- **Collection**: Shopify feed, WooCommerce API, sitemap + JSON-LD/microdata/OpenGraph, optional headless browser. Respects robots.txt, rate limits, retries.
- **Matching**: GTIN > SKU > brand+model > fuzzy title, with guards against wrong matches; unsure pairs go to a "double-check" list.
- **Analysis**: price gaps and index, discounts, stock, offers/coupons, free-shipping thresholds, return/warranty, payment options, ad pixels/tags, social links, YouTube stats, ad-library links (Meta/Google, India).
- **History**: every run is stored. Re-running shows new/removed products, price drops/rises, stock changes, new offers and price-history charts.
- **Monitors**: save a comparison to re-run every 6h/daily/weekly (while the site is running) and get email/Telegram alerts about NEW findings only.
- **AI summary**: Gemini writes from compact numbers only; any statement with a number not in the data is removed.
- **PDF report**: about 15 pages, 12 matplotlib/seaborn charts, tables, appendices. Download button on the results page.

## Docker (untested here)
`docker compose up --build` (binds to localhost). Add `INSTALL_BROWSER: "1"` in `docker-compose.yml` for the browser fallback.

## Honest limitations
- **Not yet run against real websites or in a real browser by me** (my environment has no internet access to them). Expect to tune on your first live run, especially for bot-protected sites (Cloudflare etc.).
- Gemini, YouTube, SMTP, Telegram, the headless browser and Docker are covered by tests with fakes but were **not called live**. Gemini model names/limits change; set `GEMINI_MODEL` if needed.
- Meta's official API does not return commercial ads for India, so ad data is limited to tag detection and links to the free public ad libraries. No ad spend/creatives. Instagram/Facebook numbers are not collected.
- Exact stock counts are rarely public; unknown stock is reported as unknown. Offers/policies are read by pattern matching and can miss unusual wording.
- Scraping may breach a site's or platform's terms of service. You are responsible for compliance.
- Website charts load Chart.js from cdnjs (browser needs internet). The PDF does not.
- If you upgrade from an earlier zip, the database upgrades itself; delete `data/` if you want a clean start.

## Tests
```bash
python -m pytest -q          # 141 tests, no network needed
ruff check .
# UI end-to-end (needs Node and `npm i jsdom`): start `python -m src.web --demo`, then `node scripts/ui_smoke.js`
```
