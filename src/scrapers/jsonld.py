"""Generic fallback: sitemap discovery + schema.org Product (JSON-LD / microdata) via extruct."""
from __future__ import annotations

import re
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urljoin, urlparse

import extruct
from bs4 import BeautifulSoup
from loguru import logger
from lxml import etree

from src.scrapers.base import BaseScraper, ProductRecord, ScrapeError, parse_price, valid_compare_at
from src.scrapers.heuristics import embedded_json_products, html_heuristic_product

DEFAULT_SITEMAPS = ["/sitemap.xml", "/sitemap_index.xml", "/wp-sitemap.xml", "/product-sitemap.xml", "/sitemap-index.xml", "/sitemap_products_1.xml", "/sitemap-products.xml",
                    "/product-sitemap1.xml", "/xmlsitemap.php", "/1_index_sitemap.xml", "/media/sitemap.xml", "/pub/sitemap.xml"]
LISTING_HINTS = ("/category/", "/categories/", "/collections/", "/collection/", "/product-category/", "/shop", "/store", "/catalog", "/department", "/aisle", "/c/", "/browse")
MAX_LISTING_PAGES = 30
PRODUCT_HINTS = ("/product/", "/products/", "/p/", "/item/", "/shop/", "/buy/", "/dp/")
EXCLUDE_HINTS = ("/category/", "/collections/", "/tag/", "/blog", "/cart", "/account", "/cdn-cgi/")
IMAGE_EXT = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".pdf", ".gz")
MAX_CHILD_SITEMAPS = 300
MAX_CANDIDATE_URLS = 300_000
FETCH_BATCH = 40  # pages fetched per parallel batch
GIVE_UP_AFTER = 10  # pages with no product data before we abandon this strategy

IN_STOCK = {"instock", "onlineonly", "instoreonly", "limitedavailability"}
OUT_OF_STOCK = {"outofstock", "soldout", "discontinued"}
PREORDER = {"preorder", "presale", "backorder"}


def _tail(value) -> str:
    return str(value or "").rstrip("/").rsplit("/", 1)[-1].rsplit("#", 1)[-1].strip().lower()


def map_availability(raw) -> tuple[bool | None, bool, bool]:
    """schema.org availability -> (in_stock, low_stock, is_preorder). Unknown -> in_stock None."""
    t = _tail(raw)
    if not t:
        return None, False, False
    if t in PREORDER:
        return False, False, True
    if t in OUT_OF_STOCK:
        return False, False, False
    if t in IN_STOCK:
        return True, t == "limitedavailability", False
    return None, False, False


def _as_list(v) -> list:
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


def _types(node: dict) -> set[str]:
    return {_tail(t) for t in _as_list(node.get("@type"))}


def _md_to_ld(item):
    """Normalise extruct microdata items to JSON-LD-like dicts."""
    if isinstance(item, list):
        return [_md_to_ld(i) for i in item]
    if isinstance(item, dict) and "properties" in item:
        t = item.get("type")
        t = t[0] if isinstance(t, list) and t else t
        out = {"@type": _tail(t)}
        for k, v in (item.get("properties") or {}).items():
            out[k] = _md_to_ld(v)
        return out
    return item


def _iter_nodes(obj) -> Iterator[dict]:
    if isinstance(obj, list):
        for o in obj:
            yield from _iter_nodes(o)
    elif isinstance(obj, dict):
        if "@graph" in obj:
            yield from _iter_nodes(obj["@graph"])
        yield obj


def _text(v) -> str | None:
    if isinstance(v, dict):
        v = v.get("name") or v.get("@value")
    if isinstance(v, list):
        v = v[0] if v else None
    return str(v).strip() if v not in (None, "") else None


def _image(v) -> str | None:
    v = v[0] if isinstance(v, list) and v else v
    if isinstance(v, dict):
        v = v.get("url") or v.get("contentUrl")
    return str(v) if v else None


def _parse_offers(raw) -> tuple[float | None, float | None, str | None, bool | None, bool, bool]:
    """-> (price, compare_at, currency, in_stock, low_stock, preorder), choosing the cheapest in-stock offer."""
    cands = []
    for o in _as_list(raw):
        if not isinstance(o, dict):
            continue
        nested = _as_list(o.get("offers"))
        if nested:  # AggregateOffer with child offers
            cands.extend(c for c in nested if isinstance(c, dict))
        else:
            cands.append(o)
    parsed = []
    for o in cands:
        price = parse_price(o.get("price")) or parse_price(o.get("lowPrice"))
        compare_at, currency = None, o.get("priceCurrency")
        for spec in _as_list(o.get("priceSpecification")):
            if not isinstance(spec, dict):
                continue
            sp = parse_price(spec.get("price"))
            ptype = _tail(spec.get("priceType"))
            currency = currency or spec.get("priceCurrency")
            if ptype in {"listprice", "strikethroughprice", "srp"}:
                compare_at = compare_at or sp
            elif price is None:
                price = sp
        in_stock, low, pre = map_availability(o.get("availability"))
        parsed.append((price, valid_compare_at(compare_at, price), currency, in_stock, low, pre))
    if not parsed:
        return None, None, None, None, False, False
    pool = [p for p in parsed if p[3] and p[0] is not None] or [p for p in parsed if p[0] is not None] or parsed
    best = min(pool, key=lambda p: p[0] if p[0] is not None else float("inf"))
    stocks = [p[3] for p in parsed if p[3] is not None]
    in_stock = any(stocks) if stocks else None
    return best[0], best[1], best[2], in_stock, best[4], any(p[5] for p in parsed)


def extract_products_from_html(html: str, url: str, default_currency: str) -> list[ProductRecord]:
    try:
        data = extruct.extract(html, base_url=url, syntaxes=["json-ld", "microdata"], errors="ignore")
    except Exception as exc:  # malformed markup should never crash a run
        logger.debug("extruct failed for {}: {}", url, exc)
        return []
    nodes = list(_iter_nodes(data.get("json-ld") or [])) + list(_iter_nodes(_md_to_ld(data.get("microdata") or [])))
    out: list[ProductRecord] = []
    for node in nodes:
        types = _types(node)
        if "productgroup" in types:
            for variant in _as_list(node.get("hasVariant")):
                if isinstance(variant, dict):
                    merged = {**{k: node[k] for k in ("brand", "aggregateRating", "category", "image") if k in node}, **variant}
                    rec = _node_to_record(merged, url, default_currency)
                    if rec:
                        out.append(rec)
        elif "product" in types:
            rec = _node_to_record(node, url, default_currency)
            if rec:
                out.append(rec)
    seen: set[str] = set()
    unique = []
    for r in out:  # JSON-LD and microdata often describe the same product twice
        if r.external_id not in seen:
            seen.add(r.external_id)
            unique.append(r)
    if unique:
        return unique
    og = _og_products(html, url, default_currency)
    if og:
        return og
    guess = html_heuristic_product(html, url, default_currency)  # last resort: a visible price + "Add to cart" on a plain page
    return [guess] if guess else []


def _og_products(html: str, url: str, currency: str) -> list[ProductRecord]:
    """Last resort: Open Graph / product meta tags (product:price:amount etc.)."""
    soup = BeautifulSoup(html, "lxml")

    def meta(*names: str) -> str | None:
        for n in names:
            tag = soup.find("meta", attrs={"property": n}) or soup.find("meta", attrs={"name": n})
            if tag is not None and tag.get("content"):
                return str(tag["content"]).strip()
        return None

    price = parse_price(meta("product:price:amount", "og:price:amount", "product:sale_price:amount"))
    title = meta("og:title", "twitter:title")
    if not price or not title:
        return []
    avail = meta("product:availability", "og:availability")
    in_stock, low, pre = map_availability(avail.replace(" ", "")) if avail else (None, False, False)
    compare = valid_compare_at(parse_price(meta("product:original_price:amount", "product:price:standard_amount")), price)
    page_url = meta("og:url") or url
    return [ProductRecord(external_id=page_url, url=page_url, title=title, brand=meta("product:brand", "og:brand"), image_url=meta("og:image"),
                          price=price, compare_at_price=compare, currency=meta("product:price:currency", "og:price:currency") or currency,
                          in_stock=in_stock, low_stock=low, is_preorder=pre, source_method="og_meta")]


def _node_to_record(node: dict, page_url: str, default_currency: str) -> ProductRecord | None:
    title = _text(node.get("name"))
    if not title:
        return None
    price, compare_at, currency, in_stock, low, pre = _parse_offers(node.get("offers"))
    sku = _text(node.get("sku"))
    gtin = next((_text(node.get(k)) for k in ("gtin13", "gtin", "gtin12", "gtin14", "gtin8", "isbn") if node.get(k)), None)
    agg = node.get("aggregateRating") if isinstance(node.get("aggregateRating"), dict) else {}
    rating = parse_price(agg.get("ratingValue"))
    reviews = agg.get("reviewCount") or agg.get("ratingCount")
    try:
        review_count = int(float(reviews)) if reviews is not None else None
    except (TypeError, ValueError):
        review_count = None
    product_url = _text(node.get("url")) or page_url
    return ProductRecord(
        external_id=sku or product_url,
        url=urljoin(page_url, product_url),
        title=title,
        brand=_text(node.get("brand")),
        sku=sku,
        gtin=gtin,
        mpn=_text(node.get("mpn")),
        category=_text(node.get("category")),
        image_url=_image(node.get("image")),
        price=price,
        compare_at_price=compare_at,
        currency=currency or default_currency,
        in_stock=in_stock,
        low_stock=low,
        is_preorder=pre,
        rating=rating,
        review_count=review_count,
        source_method="jsonld",
    )


# -- sitemap discovery -------------------------------------------------------
def parse_sitemap(xml_text: str) -> tuple[str, list[str]]:
    """-> ('index'|'urlset'|'unknown', [loc, ...]). Namespace-agnostic and XXE-safe."""
    parser = etree.XMLParser(resolve_entities=False, no_network=True, recover=True, huge_tree=False)
    try:
        root = etree.fromstring(xml_text.encode("utf-8"), parser=parser)
    except (etree.XMLSyntaxError, ValueError):
        return "unknown", []
    if root is None:
        return "unknown", []
    kind = etree.QName(root).localname.lower()
    # Only the page address of each <url> / <sitemap>. Shopify and other sitemaps also nest <image:loc> / <video:loc> inside every
    # <url>; counting those made the crawler download every product image as if it were a product page.
    locs = [
        (el.text or "").strip()
        for el in root.iter()
        if isinstance(el.tag, str) and etree.QName(el).localname == "loc" and (el.text or "").strip()
        and el.getparent() is not None and etree.QName(el.getparent()).localname in ("url", "sitemap")
    ]
    return ("index" if kind == "sitemapindex" else "urlset" if kind == "urlset" else "unknown"), locs


def looks_like_product_url(url: str) -> bool:
    low = url.lower().split("?")[0]
    if low.endswith(IMAGE_EXT) or any(x in low for x in EXCLUDE_HINTS):
        return False
    return any(h in low for h in PRODUCT_HINTS)


class JsonLdScraper(BaseScraper):
    method = "jsonld"

    def discover_urls(self) -> list[str]:
        declared = self.client.site_maps(self.origin)
        roots = list(dict.fromkeys([*declared, *(urljoin(self.origin, p) for p in DEFAULT_SITEMAPS)]))  # robots.txt lists first, then the usual places
        queue, visited = list(roots), set()
        product_urls: list[str] = []
        other_urls: list[str] = []
        children_seen = 0
        while queue and len(product_urls) + len(other_urls) < MAX_CANDIDATE_URLS:
            sm = queue.pop(0)
            if sm in visited or sm.lower().endswith(".gz"):
                continue
            visited.add(sm)
            res = self.client.get(sm)
            if res is None or not res.ok:
                continue
            kind, locs = parse_sitemap(res.text)
            if kind == "index":
                prod_children = [c for c in locs if "product" in c.lower()]
                for child in (prod_children or locs):
                    if children_seen < MAX_CHILD_SITEMAPS:
                        queue.append(child)
                        children_seen += 1
            elif kind == "urlset":
                is_product_map = "product" in sm.lower()
                for loc in locs:
                    if loc.lower().split("?")[0].endswith(IMAGE_EXT):  # query strings (".jpg?v=123") used to slip past this check
                        continue
                    if is_product_map:
                        product_urls.append(loc)
                    elif looks_like_product_url(loc):
                        other_urls.append(loc)
        urls = product_urls or other_urls
        if not urls:
            urls = self._homepage_links()
        if not urls:
            urls = self._crawl_listings()
        return list(dict.fromkeys(urls))

    @staticmethod
    def _page_links(html: str, base: str, host: str) -> tuple[list[str], list[str]]:
        """-> (links that look like products, links that look like category/listing pages), same site only."""
        soup = BeautifulSoup(html, "lxml")
        products: list[str] = []
        listings: list[str] = []
        for a in soup.find_all("a", href=True):
            full = urljoin(base, a["href"]).split("#")[0]
            low = full.lower().split("?")[0]
            if urlparse(full).netloc != host or low.endswith(IMAGE_EXT) or any(x in low for x in ("/cart", "/account", "/login", "/blog", "/cdn-cgi/", "/checkout")):
                continue
            in_card = any(re.search(r"product|item|card|grid-item|tile", " ".join(par.get("class", [])) + " " + str(par.get("id") or ""), re.I) for par in list(a.parents)[:4])
            if looks_like_product_url(full) or (in_card and not any(h in low for h in LISTING_HINTS)):
                products.append(full)
            elif any(h in low for h in LISTING_HINTS) or "page=" in full:
                listings.append(full)
        return products, listings

    def _crawl_listings(self) -> list[str]:
        """No sitemap and no product links on the homepage: follow the category pages (and their pagination) to find product pages."""
        home = self.fetch_page(self.origin)
        if home is None:
            return []
        host = urlparse(self.origin).netloc
        found, queue = [], []
        prods, lists = self._page_links(home[0], self.origin + "/", host)
        found += prods
        queue += list(dict.fromkeys(lists))[:12]
        seen, fetched = set(queue), 0
        while queue and fetched < MAX_LISTING_PAGES and len(found) < self.max_products * 2 and not self.out_of_time():
            url = queue.pop(0)
            page = self.fetch_page(url)
            fetched += 1
            if page is None:
                continue
            prods, lists = self._page_links(page[0], url, host)
            found += prods
            for link in lists:
                if link not in seen and len(seen) < 80 and ("page=" in link or "/page/" in link or fetched < 8):
                    seen.add(link)
                    queue.append(link)
        if found:
            logger.info("{}: no sitemap; found {} product links by following {} category pages", self.site.name, len(set(found)), fetched)
        return found

    def fetch_page(self, url: str) -> tuple[str, str] | None:
        """Return (html, final_url). Overridden by the browser scraper to render JavaScript."""
        res = self.client.get(url, use_cache=False)  # product pages are never re-read within a run; caching them only filled the disk
        return (res.text, res.url) if res is not None and res.ok else None

    def _homepage_links(self) -> list[str]:
        page = self.fetch_page(self.origin)
        if page is None:
            return []
        host = urlparse(self.origin).netloc
        soup = BeautifulSoup(page[0], "lxml")
        links = []
        for a in soup.find_all("a", href=True):
            full = urljoin(self.origin + "/", a["href"]).split("#")[0]
            if urlparse(full).netloc == host and looks_like_product_url(full):
                links.append(full)
        return links

    max_workers_override: int | None = None  # the browser scraper sets 1 (a single Chromium page is not thread-safe)

    def _workers(self) -> int:
        return self.max_workers_override or self.cfg.scraping.concurrency

    def _embedded_products(self) -> list[ProductRecord]:
        """JavaScript apps (Next.js, Nuxt...) often include their product data as JSON inside the page. Look at the homepage and a few
        category pages before concluding that a browser is needed."""
        found: dict[str, ProductRecord] = {}
        home = self.fetch_page(self.origin)
        pages = [home] if home else []
        if home:
            _, listings = self._page_links(home[0], self.origin + "/", urlparse(self.origin).netloc)
            for u in list(dict.fromkeys(listings))[:6]:
                pg = self.fetch_page(u)
                if pg:
                    pages.append(pg)
        for html, url in pages:
            for rec in embedded_json_products(html, url, self.currency):
                found.setdefault(rec.external_id, rec)
        return [r for r in found.values() if self.wants(r)][: self.max_products]

    def scrape(self) -> list[ProductRecord]:
        urls = self.discover_urls()
        if not urls:
            embedded = self._embedded_products()
            if embedded:
                self.warnings.append("products were read from data embedded in the page (no sitemap or product pages to follow); the list may be incomplete")
                logger.info("{}: {} products read from embedded page data", self.site.name, len(embedded))
                return embedded
            raise ScrapeError("no product URLs found (no usable sitemap or product links)")
        logger.info("{}: {} candidate product pages", self.site.name, len(urls))
        products: list[ProductRecord] = []
        seen: set[str] = set()
        fetched = misses = 0
        max_pages = self.max_products * 2
        workers = self._workers()

        def parse(url: str) -> list[ProductRecord]:
            page = self.fetch_page(url)
            return extract_products_from_html(page[0], page[1], self.currency) if page else []

        with ThreadPoolExecutor(max_workers=workers) as pool:
            pos = 0
            while pos < len(urls):
                if len(products) >= self.max_products or fetched >= max_pages or self.out_of_time():
                    break
                size = FETCH_BATCH if products else GIVE_UP_AFTER  # probe a few pages first: bail out fast on sites with no product data
                batch = urls[pos:pos + size]
                pos += size
                batch_results = list(pool.map(parse, batch)) if workers > 1 else [parse(u) for u in batch]
                for found in batch_results:
                    fetched += 1
                    found = [p for p in found if self.wants(p) and p.external_id not in seen]
                    if found:
                        for p in found:
                            seen.add(p.external_id)
                        products.extend(found)
                    else:
                        misses += 1
                        if not products and misses >= GIVE_UP_AFTER:
                            raise ScrapeError(f"product-style pages were found but none carried readable price data (checked {misses}: no schema.org markup, Open Graph price or visible price with an add-to-cart button)")
                self.report(len(products))
        return products[: self.max_products]
