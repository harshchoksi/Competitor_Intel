# AI Competitor Price Intelligence (website prototype)

Type your store plus up to 5 competitor URLs and click **Analyze**. The site reads **every product** on each website (no 100/500 cap),
finds the *same* product at each competitor even when it is named differently, and for each product tells you whether to **reduce, raise or keep**
your price compared with the competitor average, with a suggested price and the reason. Results also include discounts, stock, offers,
changes since last time, alerts and a PDF report. There is **no login**: run it on your own computer or behind a firewall.

## Quick start (Python 3.11+)
```bash
python -m venv .venv && source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python -m src.web --demo                                    # open http://localhost:8000
```
In the page: **Fill demo URLs** > **Analyze**. The demo is six fake grocery stores (~120 products each) that word every product and pack size
differently, so you can watch the matching work. For real sites run `python -m src.web` and type real URLs (use each store's homepage).

## What it does for each product
Example: your site sells "Chicken Breast 500g" at 120, competitors sell it at 100 and 100 (one listed as "CHICKEN BREAST FARMCO, 0.5 kg").
- **Match**: found by meaning, not exact words (see below).
- **Market average** = average of the matched competitor prices, scaled to *your* pack size. Out-of-stock competitor prices are ignored if an in-stock price exists.
- **Gap**: you are 20% above the average.
- **Recommendation**: *Reduce price* 120 -> 99 (1% under the average). If you were far *below* the average it says *Raise price* by at most 5% (never above the market); within +/-3% it says *Keep price*. All three numbers are options.
- Each card shows the matched competitor products (with links), why they were matched, a confidence level and warnings (only one competitor, prices vary wildly, sizes were scaled...).
- Products are also grouped by **type** (Meat & Poultry, Dairy & Eggs, Drinks, Headphones...) so you can see, for example, "meat: you are 6% above competitors, reduce 9 of 14 products". Click a type to see its products.
- Filter by type, brand, competitor, action, price range, match %, stock; sort; page through thousands of products; **Export CSV**.

## How matching works (src/analysis/semantic.py)
1. **Identifiers first**: barcode (GTIN), SKU/MPN, model numbers.
2. **Language**: plurals and synonyms are folded together (prawn = shrimp, aubergine = eggplant, mince = ground, yoghurt = yogurt, "Mac & Cheese" = "Macaroni and Cheese"), pack sizes are parsed and normalised (500g = 0.5 kg = 1.1 lb; 33cl = 330 ml; 8 oz = 8 fl oz) and then taken out of the words being compared. Words are weighted by rarity (the brand counts more than "fresh") and combined with fuzzy text similarity.
3. **Logic guards** so look-alikes never match: different meats (chicken vs turkey), cuts (breast vs thigh), diet vs regular, men vs women, different brands, conflicting model numbers or specs are rejected; different flavours, forms (frozen/canned), fat levels, boneless vs bone-in are penalised; incompatible product types are penalised.
4. **Pack size**: same size is a plus. A different size of the same product still matches and is compared **per unit**.
5. **Optional embeddings** (`pip install -r requirements-semantic.txt`): sentence-embedding similarity finds links the dictionary does not know (e.g. couch/sofa) and adds extra candidates.
6. **Optional AI check**: with a Gemini key, borderline pairs (between the review floor and your threshold) are sent in small batches to Gemini for a yes/no "same product?"; yes promotes the pair, no drops it. Capped at 120 pairs per competitor.
Matching is one-to-one per competitor. The **match threshold** slider (default 80%) trades more matches against more certainty; pairs below it appear under *Data quality > Possible matches to double-check*.

## Reading ALL the products
- Shopify (`/products.json`) and WooCommerce (Store API) are paged until the site stops returning products, with loop protection.
- Other sites: sitemaps (up to 300 sub-sitemaps / 300,000 URLs) and schema.org JSON-LD/microdata, fetched in parallel (4 at a time by default; the per-site rate limit still applies, default 2 requests/second, adjustable under Options).
- A per-site safety time budget (45 min) and ceiling (200,000 products) stop runaway crawls. If a site hits the time budget you get the products read so far and a note in *Data quality*.
- **Max products per site** is optional: leave it empty for everything. **Only these product types / keywords** limits the run to e.g. `meat, chicken`.
- Live progress shows how many products have been found per site. Big catalogues take a while: a generic site needs one request per product page, so 5,000 products at 2 requests/second is about 40 minutes (Shopify/WooCommerce sites are far faster).

## Competitor promotions (discount amount, product type, period)
The **Competitor promotions** tab answers: how much do competitors discount, on what, and for how long. Everything is read from the competitors' own websites:
- **Discount amount and product type**: every product with a crossed-out "was" price. Shown as average / maximum % off, share of the catalogue on sale, and a product-type matrix (you vs each competitor, with the gap in percentage points). Competitors that do not publish "was" prices are listed as "not measurable".
- **Campaigns**: homepage banners, announcement bars, hero text and pop-ups are split into separate offers and parsed into amount (% / flat / buy-one-get-one / free shipping), what it applies to (a product type or everything), conditions (first order, min spend...), coupon code and **period** (stated start/end dates, "today only", "this weekend", "ends Sunday"). Days left are calculated for you.
- **Observed duration**: each analysis stores what it saw, so from the second run onward the tab shows how long today's discounts have actually been in place (a lower bound: the sale may have started before our first look). Run it again later, or use a monitor, to build this up.
- **Export**: promotions CSV; a "Competitor promotions" section is also added to the PDF.
- **What is NOT included**: Instagram/Facebook/TikTok posts and paid ads (planned as the next phase). Banners loaded only by JavaScript cannot be read. Dates written as images or without a day/month are not understood; numeric dates are read day/month.

## Price sanity checks
- **Multipacks** are read as unit size x count ("12oz, 24ct" is 24 x 12 oz, not 12 oz). A 24-pack is never compared with a single can; a 12-pack versus a 24-pack is compared per unit.
- **Implausible gaps are kept out.** If a competitor's price (scaled to your pack size) is more than 3x above or below yours, that match is ignored in the averages and listed under Data quality > "Matches ignored because prices were far apart", so one bad match can't produce a +2000% recommendation. Change the limit with the `max_price_ratio` field.
- **Pack sizes outside the title.** Many shops put the size on the variant ("PER 500 GR"), in an attribute, or in the description. The scrapers read those (the variant the price comes from first; shipping weight only as a weak last resort), and "per kg" wording counts as a 1 kg pack. If a size is stated on only one site, the match is NOT averaged into a recommendation (it appears under Data quality with the reason); untick "Only compare products whose pack size is known" in Options to include such matches anyway.
- Each product card shows your product (price, was-price, stock, unit price) beside every matched competitor product (price, was-price, stock, size, match %, link).

## Alerts: email and Telegram (the "Monitor this comparison" box)
A monitor re-runs your comparison on a schedule and sends a digest when something changed (price drops, new offers, new products, undercuts). It only runs **while the website is running**, and **no message is sent if nothing changed** (the first run has nothing to compare with, so don't expect an email from it).

**Email (needs an SMTP account).** Put these in `.env` (on your PC) or in Render's *Environment* tab (on Render; `.env` is not deployed):
```
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=you@gmail.com
SMTP_PASSWORD=your-16-letter-app-password
SMTP_FROM=you@gmail.com
APP_BASE_URL=https://your-app.onrender.com     # makes the "Full report" link in the email work
```
- **Gmail:** a normal password does not work. Turn on 2-Step Verification, then create an *App password* (Google Account > Security > App passwords) and paste the 16 letters (no spaces) as `SMTP_PASSWORD`. Port 587 uses STARTTLS; port 465 uses SSL; both are supported.
- Other providers work the same way (host, port, login). Use a transactional-email service (Brevo, Mailgun, SendGrid...) if you want to send from your own domain; check their current free limits.
- Restart the app after changing `.env`.

**Telegram.** In Telegram open **@BotFather**, send `/newbot`, follow the steps and copy the token into `TELEGRAM_BOT_TOKEN`. Then open a chat with your new bot and press **Start**. Your chat id is the number shown by **@userinfobot**, or open `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates` after messaging the bot and read `"chat":{"id": ...}`. Type that number in the "Telegram chat id" box. Keep the token private.

**Test before relying on it:**
```bash
python scripts/test_alerts.py --email you@example.com
python scripts/test_alerts.py --telegram 123456789
```
It prints which settings it sees and, if sending fails, a plain-language hint (wrong app password, wrong host/port...).

## Hosting (Render etc.)
See **DEPLOY_RENDER.md** for why a hosted run can be much slower than on your computer, the database errors seen on hosts, the settings to use, and `render.yaml`.

## Optional settings (copy `.env.example` to `.env`)
| Feature | Setting | Cost |
|---|---|---|
| AI summary (Gemini) | `GEMINI_API_KEY` (Google AI Studio) | Free tier; check Google's terms on how free-tier data may be used |
| YouTube stats | `YOUTUBE_API_KEY` | Free quota |
| Email alerts | `SMTP_HOST/PORT/USER/PASSWORD/FROM` | Your mail provider |
| Telegram alerts | `TELEGRAM_BOT_TOKEN`, then chat id in the monitor form | Free |
| Smarter matching (embeddings) | `pip install -r requirements-semantic.txt` (optional `SEMANTIC_MODEL`) | Free, ~100 MB |
| JavaScript-rendered sites | `pip install -r requirements-browser.txt && playwright install chromium` | Free, ~400 MB |

Without keys everything else still works; the AI card is simply hidden and rule-based insights are shown.

## What is in it
- **Collection**: Shopify feed, WooCommerce API, sitemap + JSON-LD/microdata/OpenGraph, optional headless browser. Respects robots.txt, rate limits, retries.
- **Currency**: read from each site (Shopify/OpenGraph/schema.org) or set it in Options. Prices are **not converted**: if a competitor uses a different currency you get a warning.
- **Analysis**: price gaps and index, discounts, stock, offers/coupons, free-shipping thresholds, return/warranty, payment options, ad pixels/tags, social links, YouTube stats, ad-library links (Meta/Google, India).
- **History**: every run is stored. Re-running shows new/removed products, price drops/rises, stock changes, new offers and price-history charts.
- **Monitors**: save a comparison to re-run every 6h/daily/weekly (while the site is running) and get email/Telegram alerts about NEW findings only.
- **AI summary**: Gemini writes from compact numbers only; any statement with a number not in the data is removed.
- **PDF report**: about 15 pages, 12 matplotlib/seaborn charts, tables, appendices. Download button on the results page.

## Docker (untested here)
`docker compose up --build` (binds to localhost). Add `INSTALL_BROWSER: "1"` in `docker-compose.yml` for the browser fallback.

## Honest limitations
- **Not yet run against real websites or in a real browser by me** (my environment has no internet access to them). I tested everything against the built-in demo stores, mocked HTTP and a 4,000 x 5 synthetic benchmark (matching and pricing took ~17 s). Expect to tune on your first live run, especially for bot-protected sites (Cloudflare etc.).
- **Matching accuracy**: on my test pairs it matched true pairs written differently and rejected look-alikes, but no matcher is perfect. Generic-vs-specific titles ("Tea Bags" vs "Green Tea Bags") can slip through; always look at the confidence and the "why matched" line before repricing. Raise the threshold for strictness.
- **No image matching** (the reference site also compares product images). Matching uses text, identifiers, pack size and optional embeddings.
- Gemini (match check, summary), the embedding model, YouTube, SMTP, Telegram, the headless browser and Docker are covered by tests with fakes but were **not called live**. Gemini model names/limits change; set `GEMINI_MODEL` if needed.
- Recommendations are a pricing *signal from competitor prices only*. They know nothing about your costs, margins, promotions or brand positioning. Check margins first.
- Competitor sites that need a login, show prices only after choosing a location/store, or load products only via JavaScript may not be readable (install the browser fallback for the latter).
- Meta's official API does not return commercial ads for India, so ad data is limited to tag detection and links to the free public ad libraries. Exact stock counts are rarely public; unknown stock is reported as unknown.
- Shipping/return/offer detection is tuned to Indian (INR) wording; for other regions those cards may be empty.
- Scraping may breach a site's or platform's terms of service. You are responsible for compliance.
- Website charts load Chart.js from cdnjs (browser needs internet). The PDF does not (its price charts still label axes in rupees).
- If you upgrade from an earlier zip, the database upgrades itself; delete `data/` if you want a clean start.

## Tests
```bash
python -m pytest -q          # 262 tests, no network needed
ruff check .
# UI end-to-end (needs Node and `npm i jsdom`): start `python -m src.web --demo`, then `node scripts/ui_smoke.js`
```
