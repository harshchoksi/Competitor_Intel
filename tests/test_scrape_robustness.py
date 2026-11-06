"""Sites that could not be read: finding the real store, category crawling, proxy fallback and plain-language reasons."""
import json

import httpx
import pytest

from src.config import AppConfig, ScrapingSettings, SiteConfig
from src.scrapers.diagnose import explain_no_products, explain_unreachable
from src.scrapers.discovery import find_store_candidates, guess_store_origins, registrable_domain
from src.scrapers.http import PoliteClient
from src.scrapers.runner import scrape_site

SHOPIFY_HOME = '<html><script src="https://cdn.shopify.com/s/x.js"></script></html>'


def products_json(n=6):
    return json.dumps({"products": [{"id": i + 1, "title": f"Item {i}", "handle": f"i{i}", "vendor": "V", "variants": [{"id": i, "price": "5.00", "available": True}]} for i in range(n)]})


def make_client(handler, **kw):
    s = ScrapingSettings(respect_robots=False, cache_ttl_minutes=0, rate_limit_per_sec=50)
    return PoliteClient(s, transport=httpx.MockTransport(handler), sleep=lambda x: None, **kw)


def cfg_for(url):
    return AppConfig(source=SiteConfig(name="Ling", url=url), competitors=[SiteConfig(name="Other", url="https://other.test")], scraping=ScrapingSettings(respect_robots=False, cache_ttl_minutes=0, rate_limit_per_sec=50))


# ---------------------------------------------------------------- candidates
def test_store_candidates_from_homepage_links_and_guesses():
    html = ('<nav><a href="/about">About</a><a href="https://shop.lingandsons.com/">Shop Online</a><a href="https://www.facebook.com/x">Facebook</a>'
            '<a href="https://lingandsons.com/contact">Contact</a><a href="https://random-blog.net/post">Our blog</a></nav>')
    assert find_store_candidates(html, "https://lingandsons.com") == ["https://shop.lingandsons.com"]
    assert guess_store_origins("lingandsons.com")[0] == "https://shop.lingandsons.com" and guess_store_origins("shop.lingandsons.com") == []
    assert registrable_domain("www.shop.example.co.uk") == "example.co.uk"


# ---------------------------------------------------------------- the lingandsons.com situation
def test_main_website_without_products_falls_back_to_the_linked_store():
    def handler(request: httpx.Request) -> httpx.Response:
        host, path = request.url.host, request.url.path
        if host == "lingandsons.com":
            return httpx.Response(200, text='<html><body><a href="https://shop.lingandsons.com/">Shop Online</a><p>Welcome to Ling & Sons</p></body></html>') if path == "/" else httpx.Response(404)
        if host == "shop.lingandsons.com":
            if path == "/":
                return httpx.Response(200, text=SHOPIFY_HOME)
            if path == "/products.json":
                return httpx.Response(200, text=products_json(6) if request.url.params.get("page", "1") == "1" else json.dumps({"products": []}))
        return httpx.Response(404)

    cfg = cfg_for("https://lingandsons.com")
    res = scrape_site(cfg.source, False, cfg, make_client(handler))
    assert res.status == "ok" and len(res.products) == 6 and res.method == "shopify_products_json"
    assert res.resolved_url == "https://shop.lingandsons.com" and res.site.url.startswith("https://lingandsons.com")
    assert any("online store" in w and "shop.lingandsons.com" in w for w in res.warnings)


def test_store_found_by_guessing_the_shop_subdomain_when_nothing_is_linked():
    def handler(request: httpx.Request) -> httpx.Response:
        host, path = request.url.host, request.url.path
        if host == "acme.com":
            return httpx.Response(200, text="<html><body><p>We sell great things. Visit us!</p></body></html>") if path == "/" else httpx.Response(404)
        if host == "shop.acme.com":
            if path == "/":
                return httpx.Response(200, text=SHOPIFY_HOME)
            if path == "/products.json":
                return httpx.Response(200, text=products_json(3) if request.url.params.get("page", "1") == "1" else json.dumps({"products": []}))
        raise httpx.ConnectError("no such host")  # store./order. do not exist

    cfg = cfg_for("https://acme.com")
    res = scrape_site(cfg.source, False, cfg, make_client(handler))
    assert res.status == "ok" and res.resolved_url == "https://shop.acme.com" and len(res.products) == 3


def test_no_store_anywhere_gives_a_clear_hint():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "acme.com" and request.url.path == "/":
            return httpx.Response(200, text="<html><body><p>Hello</p></body></html>")
        if request.url.host == "acme.com":
            return httpx.Response(404)
        raise httpx.ConnectError("no such host")

    cfg = cfg_for("https://acme.com")
    res = scrape_site(cfg.source, False, cfg, make_client(handler))
    assert res.status != "ok" and "main website rather than its online store" in res.hint and "https://shop.yourcompany.com" in res.error
    assert "technical details" in res.error


# ---------------------------------------------------------------- no sitemap: follow category pages
def test_products_found_by_following_category_pages_when_there_is_no_sitemap():
    def page(title, n):
        return (f'<html><script type="application/ld+json">{{"@type":"Product","name":"{title}","sku":"{title}","offers":{{"@type":"Offer","price":"{n}.00","priceCurrency":"USD"}}}}</script></html>')

    def handler(request: httpx.Request) -> httpx.Response:
        p = request.url.path
        if p == "/":
            return httpx.Response(200, text='<html><nav><a href="/category/meat">Meat</a><a href="/category/dairy">Dairy</a></nav></html>')
        if p.startswith("/category/"):
            kind = p.split("/")[-1]
            return httpx.Response(200, text="<html>" + "".join(f'<div class="product-card"><a href="/item/{kind}-{i}">x</a></div>' for i in range(4)) + "</html>")
        if p.startswith("/item/"):
            return httpx.Response(200, text=page(p.split("/")[-1], 5))
        return httpx.Response(404)

    cfg = cfg_for("https://plain.test")
    res = scrape_site(cfg.source, False, cfg, make_client(handler))
    assert res.status == "ok" and res.method == "jsonld" and len(res.products) == 8


# ---------------------------------------------------------------- proxy fallback
def test_blocked_requests_are_retried_through_the_proxy_only_when_configured():
    def direct(request):
        return httpx.Response(403, text="Access denied")

    def via_proxy(request):
        return httpx.Response(200, text="<html>real page</html>", headers={"content-type": "text/html"})

    plain = make_client(direct)
    assert plain.get("https://blocked.test/").status_code == 403 and not plain.proxy_configured

    s = ScrapingSettings(respect_robots=False, cache_ttl_minutes=0, rate_limit_per_sec=50)
    c = PoliteClient(s, transport=httpx.MockTransport(direct), proxy_transport=httpx.MockTransport(via_proxy), sleep=lambda x: None)
    r = c.get("https://blocked.test/")
    assert r.status_code == 200 and "real page" in r.text and "blocked.test" in c.proxy_hosts

    ok = PoliteClient(s, transport=httpx.MockTransport(via_proxy), proxy_transport=httpx.MockTransport(direct), sleep=lambda x: None)
    assert ok.get("https://fine.test/").status_code == 200 and not ok.proxy_hosts  # a working site never touches the proxy


def test_failure_reasons_are_recorded_per_host():
    c = make_client(lambda r: (_ for _ in ()).throw(httpx.ConnectError("down")))
    assert c.get("https://gone.test/") is None and c.failures["gone.test"] == "network"


# ---------------------------------------------------------------- plain-language reasons
@pytest.mark.parametrize("evidence,robots,expect", [
    ("homepage HTTP 403", False, "refused this server"),
    ("homepage HTTP 404", False, "page not found"),
    ("homepage unreachable or blocked by robots.txt", True, "robots.txt"),
    ("homepage unreachable or blocked by robots.txt", False, "did not answer"),
])
def test_explain_unreachable(evidence, robots, expect):
    assert expect in explain_unreachable(evidence, robots, False)
    assert "SCRAPER_PROXY_URL" in explain_unreachable("homepage HTTP 403", False, False) and "SCRAPER_PROXY_URL" not in explain_unreachable("homepage HTTP 403", False, True)


def test_explain_no_products_recognises_js_apps_and_bot_checks():
    assert "JavaScript" in explain_no_products('<div id="root"></div><script src="app.js"></script>', [], [])
    assert "bot-check" in explain_no_products("<title>Just a moment...</title>", [], [])
    assert "https://shop.yourcompany.com" in explain_no_products("<p>hi</p>" + "<a href=x>l</a>" * 40, ["shop.a.com"], [])


# ---------------------------------------------------------------- faster feed reading
def test_feed_pages_are_fetched_in_parallel_without_over_reading(make_client, cfg):
    """A 600-product Shopify feed = 3 pages. Fetched in one wave: every product exactly once, in order, progress per page."""
    from src.scrapers.shopify import ShopifyScraper

    cfg.max_products_per_site = None
    requested = []

    def route(request):
        requested.append(int(request.url.params.get("page", 1)))
        page = int(request.url.params.get("page", 1))
        ids = range((page - 1) * 250, min(page * 250, 600))
        return httpx.Response(200, json={"products": [{"id": i + 1, "title": f"P{i}", "handle": f"p{i}", "variants": [{"id": i, "price": "1.00", "available": True}]} for i in ids]})

    seen = []
    sc = ShopifyScraper(make_client({"/products.json": route}), cfg.source, cfg)
    sc.progress_cb = seen.append
    recs = sc.scrape()
    assert len(recs) == 600 and [r.external_id for r in recs] == [str(i + 1) for i in range(600)]
    assert seen == [250, 500, 600] and max(requested) <= 3  # no wasted requests beyond the first wave


def test_feeds_use_the_faster_feed_rate_but_crawling_keeps_the_polite_rate():
    waits = []
    c = PoliteClient(ScrapingSettings(respect_robots=False, cache_ttl_minutes=0, rate_limit_per_sec=2), transport=httpx.MockTransport(lambda r: httpx.Response(200, text="ok")),
                     sleep=waits.append, monotonic=lambda: 0.0)
    c.get("https://a.test/1")
    c.get("https://a.test/2")
    assert waits and abs(waits[-1] - 0.5) < 1e-9  # 2 requests/second
    waits.clear()
    c2 = PoliteClient(ScrapingSettings(respect_robots=False, cache_ttl_minutes=0, rate_limit_per_sec=2), transport=httpx.MockTransport(lambda r: httpx.Response(200, text="ok")),
                      sleep=waits.append, monotonic=lambda: 0.0)
    c2.get("https://b.test/1", rate=5)
    c2.get("https://b.test/2", rate=5)
    assert waits and abs(waits[-1] - 0.2) < 1e-9  # 5 requests/second for a JSON feed


# ---------------------------------------------------------------- quickmart: robots.txt that is only TEMPORARILY unreachable
def _robots_client(handler, waits=None):
    s = ScrapingSettings(respect_robots=True, cache_ttl_minutes=0, rate_limit_per_sec=50)
    return PoliteClient(s, transport=httpx.MockTransport(handler), sleep=(waits.append if waits is not None else (lambda x: None)))


def test_robots_that_fails_twice_then_works_does_not_block_the_site():
    """quickmartaruba.com: succeeded in 3 of 5 runs and failed fast in 2. A busy server must not be mistaken for a ban."""
    calls = {"robots": 0}

    def handler(request):
        if request.url.path == "/robots.txt":
            calls["robots"] += 1
            return httpx.Response(503) if calls["robots"] <= 2 else httpx.Response(200, text="User-agent: *\nDisallow: /cart\n")
        return httpx.Response(200, text="<html>home</html>")

    waits = []
    c = _robots_client(handler, waits)
    assert c.get("https://q.test/").status_code == 200 and calls["robots"] == 3 and [w for w in waits if w >= 1] == [2, 5]
    assert c.get("https://q.test/cart/x") is None and c.failures["q.test"] == "robots"  # a real Disallow is still honoured


def test_persistently_unreachable_robots_is_reported_as_unreachable_not_forbidden_and_recovers():
    state = {"up": False}
    now = {"t": 0.0}

    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow:\n") if state["up"] else httpx.Response(429, headers={"retry-after": "1"})
        return httpx.Response(200, text="<html>home</html>")

    s = ScrapingSettings(respect_robots=True, cache_ttl_minutes=0, rate_limit_per_sec=50)
    c = PoliteClient(s, transport=httpx.MockTransport(handler), sleep=lambda x: None, monotonic=lambda: now["t"])
    assert c.get("https://q.test/") is None
    assert c.failures["q.test"].startswith("robots-unreachable:HTTP 429")
    msg = explain_unreachable("homepage unreachable or blocked by robots.txt", c.failures["q.test"], False)
    assert "could not be downloaded" in msg and "not a ban" in msg and "asks automated tools" not in msg
    state["up"] = True
    now["t"] = 31.0  # the verdict expires; the next request asks again
    assert c.get("https://q.test/").status_code == 200


def test_site_is_retried_once_when_robots_was_only_unreachable():
    calls = {"robots": 0}

    def handler(request):
        path = request.url.path
        if path == "/robots.txt":
            calls["robots"] += 1
            return httpx.Response(503) if calls["robots"] <= 4 else httpx.Response(404)  # first full round of tries fails, the retry works
        if path == "/":
            return httpx.Response(200, text=SHOPIFY_HOME)
        if path == "/products.json":
            return httpx.Response(200, text=products_json(4) if request.url.params.get("page", "1") == "1" else json.dumps({"products": []}))
        return httpx.Response(404)

    s = ScrapingSettings(respect_robots=True, cache_ttl_minutes=0, rate_limit_per_sec=50)
    cfg = AppConfig(source=SiteConfig(name="Quick", url="http://q.test"), competitors=[SiteConfig(name="O", url="https://o.test")], scraping=s)
    res = scrape_site(cfg.source, False, cfg, PoliteClient(s, transport=httpx.MockTransport(handler), sleep=lambda x: None))
    assert res.status == "ok" and len(res.products) == 4


# ---------------------------------------------------------------- sites without schema.org markup / built with JavaScript frameworks
from src.scrapers.heuristics import embedded_json_products, html_heuristic_product  # noqa: E402

PLAIN_PAGE = """<html><head><meta property="og:image" content="/img/s.jpg"></head><body><h1>Fresh Salmon Fillet 400g</h1>
<div class="product-price"><del class="old-price">$14.99</del> <span class="price">$11.99</span></div><p>SKU: SAL-400</p><button>Add to cart</button></body></html>"""


def test_plain_product_page_without_markup_is_read_from_visible_html():
    r = html_heuristic_product(PLAIN_PAGE, "https://s.test/p/salmon", "USD")
    assert (r.title, r.price, r.compare_at_price, r.sku, r.in_stock) == ("Fresh Salmon Fillet 400g", 11.99, 14.99, "SAL-400", True)
    assert r.image_url == "https://s.test/img/s.jpg"
    assert html_heuristic_product("<html><h1>Our story</h1><p>Founded 1998. Lunch from $5.</p></html>", "https://s.test/about", "USD") is None  # no cart cue
    oos = html_heuristic_product(PLAIN_PAGE.replace("Add to cart", "Sold out"), "https://s.test/p/x", "USD")
    assert oos.in_stock is False


def _next_page(n):
    data = {"props": {"pageProps": {"items": [{"id": i, "name": f"Item {i}", "price": {"amount": i + 1.5}, "image": {"url": f"/i{i}.jpg"}, "available": i % 2 == 0} for i in range(n)]}}}
    return f'<html><script id="__NEXT_DATA__" type="application/json">{json.dumps(data)}</script><body><div id="__next"></div></body></html>'


def test_embedded_app_data_is_read_and_stray_matches_are_ignored():
    ps = embedded_json_products(_next_page(6), "https://app.test/", "USD")
    assert len(ps) == 6 and ps[1].price == 2.5 and ps[1].in_stock is False and ps[0].image_url == "https://app.test/i0.jpg"
    assert embedded_json_products(_next_page(2), "https://app.test/", "USD") == []  # fewer than 4: not a catalogue
    assert embedded_json_products('<script>window.__STATE__ = {"a":[{"id":1,"name":"Thing","price":3}]};</script>', "https://x/", "USD") == []


def test_javascript_app_site_is_read_from_its_embedded_data_end_to_end():
    def handler(request):
        if request.url.path == "/":
            return httpx.Response(200, text=_next_page(8))
        return httpx.Response(404)

    cfg = cfg_for("https://bluelike.test")
    res = scrape_site(cfg.source, False, cfg, make_client(handler))
    assert res.status == "ok" and len(res.products) == 8 and any("embedded in the page" in w for w in res.warnings)


def test_store_with_no_schema_markup_is_read_through_visible_prices():
    def handler(request):
        p = request.url.path
        if p == "/sitemap.xml":
            return httpx.Response(200, text='<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">' + "".join(f"<url><loc>https://plain.test/product/{i}</loc></url>" for i in range(5)) + "</urlset>",
                                  headers={"content-type": "application/xml"})
        if p.startswith("/product/"):
            return httpx.Response(200, text=PLAIN_PAGE.replace("Salmon", f"Salmon {p[-1]}").replace("SAL-400", f"SAL-{p[-1]}"))
        return httpx.Response(200, text="<html>home</html>") if p == "/" else httpx.Response(404)

    cfg = cfg_for("https://plain.test")
    res = scrape_site(cfg.source, False, cfg, make_client(handler))
    assert res.status == "ok" and len(res.products) == 5 and res.products[0].price == 11.99


def test_pages_with_no_readable_price_explain_that_the_site_may_not_sell_online():
    def handler(request):
        if request.url.path == "/sitemap.xml":
            return httpx.Response(200, text='<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">' + "".join(f"<url><loc>https://nostore.test/product/{i}</loc></url>" for i in range(12)) + "</urlset>",
                                  headers={"content-type": "application/xml"})
        if request.url.path.startswith("/product/"):
            return httpx.Response(200, text="<html><h1>Catalogue item</h1><p>Call us to order</p></html>")
        return httpx.Response(200, text="<html>home</html>") if request.url.path == "/" else httpx.Response(404)

    cfg = cfg_for("https://nostore.test")
    res = scrape_site(cfg.source, False, cfg, make_client(handler))
    assert res.status != "ok" and "may not sell online" in res.error and "readable price data" in res.error
