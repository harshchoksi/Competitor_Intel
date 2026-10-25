from types import SimpleNamespace

from src.analysis.metrics import analyze
from src.analysis.types import SiteData
from src.scrapers.browser import BrowserScraper, browser_enabled
from src.scrapers.jsonld import extract_products_from_html
from src.scrapers.signals import (
    classify_offer,
    collect_site_signals,
    detect_stack,
    extract_policies,
    extract_signals,
    parse_shipping,
)

HOME = """<html><head>
<script src="https://connect.facebook.net/en_US/fbevents.js"></script><script>fbq('init','123');</script>
<script async src="https://www.googletagmanager.com/gtag/js?id=G-ABCDEF1234"></script><script>gtag('config','AW-1234567890')</script>
<script src="https://cdn.razorpay.com/x.js"></script></head><body>
<div class="announcement-bar">Flat 20% OFF on all headphones. Use code SAVE20 at checkout</div>
<div class="topbar">Free shipping on orders above ₹999</div>
<p>HDFC Bank offer: extra 10% off on credit cards. No cost EMI available. Pay via UPI or Cash on Delivery.</p>
<nav><a href="/">Home</a><a href="/policies/refund-policy">Refund policy</a><a href="/products/x">Shipping bag</a><a href="/pages/warranty">Warranty</a></nav>
<footer><a href="https://www.instagram.com/mystore/">IG</a><a href="https://www.instagram.com/p/abc/">post</a>
<a href="https://www.facebook.com/sharer/sharer.php?u=x">share</a><a href="https://facebook.com/mystorepage">FB</a>
<a href="https://www.youtube.com/@mystore">YT</a><a href="https://twitter.com/intent/tweet">t</a></footer></body></html>"""


def test_offers_coupons_and_shipping():
    sig = extract_signals(HOME, "https://s.test")
    kinds = {o["type"] for o in sig["offers"]}
    assert {"coupon", "free_shipping", "bank_offer"} <= kinds
    coupon = next(o for o in sig["offers"] if o["type"] == "coupon")
    assert coupon["code"] == "SAVE20" and coupon["discount_value"] == 20.0
    assert sig["free_shipping_threshold"] == 999 and not sig["free_shipping_all"]
    assert {"UPI", "Cash on delivery", "No-cost EMI", "EMI"} <= set(sig["payment_methods"])


def test_social_links_skip_share_and_post_urls():
    s = extract_signals(HOME, "https://s.test")["social_links"]
    assert s["instagram"]["handle"] == "mystore" and s["facebook"]["handle"] == "mystorepage" and s["youtube"]["handle"] == "@mystore"
    assert "twitter" not in s


def test_marketing_stack_detection():
    stack = detect_stack(HOME)
    assert stack["advertising"] == ["Meta Pixel", "Google Ads tag"] and "Google Analytics 4" in stack["analytics"] and stack["checkout"] == ["Razorpay"]
    assert detect_stack("<html>plain</html>") == {}


def test_policy_links_exclude_product_pages():
    links = extract_signals(HOME, "https://s.test")["policy_links"]
    assert "https://s.test/policies/refund-policy" in links and not any("/products/" in u for u in links)


def test_policy_parsing():
    p = extract_policies("We offer a 7-day easy return. All products carry 1 year manufacturer warranty. Shipping charges are ₹49 per order.")
    assert p == {"return_window_days": 7, "warranty": "1 year", "shipping_fee": 49}
    assert extract_policies("Refunds are processed if requested within 30 days of delivery")["return_window_days"] == 30
    assert extract_policies("nothing relevant here") == {"return_window_days": None, "warranty": None, "shipping_fee": None}


def test_shipping_variants():
    assert parse_shipping("Free delivery on orders over Rs. 1,499") == (1499, False)
    assert parse_shipping("FREE SHIPPING ON ALL ORDERS") == (0, True)
    assert parse_shipping("shipping is charged at checkout") == (None, False)


def test_classify_ignores_generic_words_as_codes():
    assert classify_offer("Use code at checkout for free shipping")["code"] is None
    assert classify_offer("Buy 2 get 1 free")["type"] == "bundle"


def test_collect_site_signals_reads_policy_pages(make_client):
    c = make_client({"/": (200, HOME), "/policies/refund-policy": (200, "<p>Returns accepted within 10 days. 6 month warranty on all items.</p>"),
                     "/pages/warranty": (200, "<p>Warranty details</p>")})
    sig = collect_site_signals(c, "https://s.test")
    assert sig["policies"]["return_window_days"] == 10 and sig["policies"]["warranty"] == "6 months" and len(sig["policy_pages"]) == 2


def test_collect_site_signals_never_raises(make_client):
    assert collect_site_signals(make_client({"/": (503, "x")}), "https://down.test") is None


def test_og_meta_fallback():
    html = ('<html><head><meta property="og:title" content="Nova Lamp"><meta property="product:price:amount" content="1,299">'
            '<meta property="product:price:currency" content="INR"><meta property="product:availability" content="out of stock">'
            '<meta property="product:original_price:amount" content="1599"></head></html>')
    (rec,) = extract_products_from_html(html, "https://s.test/p/lamp", "INR")
    assert rec.price == 1299.0 and rec.compare_at_price == 1599.0 and rec.in_stock is False and rec.source_method == "og_meta"
    assert extract_products_from_html("<html><meta property='og:title' content='No price'></html>", "https://s.test/", "INR") == []


def test_signals_flow_into_insights_and_recommendations():
    from src.scrapers.base import ProductRecord

    def mk(key, source, ship, ret=None, ads=False):
        sig = {"offers": [{"type": "coupon", "text": "x", "code": "SAVE10", "discount_value": 10.0}] if not source else [], "free_shipping_threshold": ship,
               "free_shipping_all": False, "payment_methods": [], "social_links": {}, "policies": {"return_window_days": ret},
               "marketing_stack": {"advertising": ["Meta Pixel"]} if ads else {}}
        return SiteData(key=key, name=key, is_source=source, status="ok", signals=sig,
                        products=[ProductRecord(external_id="1", title="Sonex HP-100 Headphone", brand="Sonex", price=100.0)])

    r = analyze([mk("me", True, 999, 7), mk("rival", False, 499, 30, ads=True)])
    titles = {i["title"] for i in r["insights"]}
    assert {"Free shipping", "Coupon codes", "Ad tracking found", "Return windows"} <= titles
    assert any(x["action"] == "Review your free-shipping threshold" for x in r["recommendations"])
    assert any(x["action"] == "Consider a longer return window" for x in r["recommendations"])
    assert "rival" in r["signals"]


class FakePage:
    def __init__(self, html="<html></html>", fail=False):
        self.html, self.fail, self.url, self.visited = html, fail, "", []

    def goto(self, url, **kw):
        if self.fail:
            raise RuntimeError("boom")
        self.url = url
        self.visited.append(url)

    def wait_for_load_state(self, *a, **kw):
        raise TimeoutError("never idle")  # must be tolerated

    def content(self):
        return self.html


def test_browser_scraper_renders_pages_through_page_object(make_client, cfg):
    c = make_client({})
    s = BrowserScraper(c, cfg.competitors[0], cfg)
    s._page = FakePage("<html>rendered</html>")
    assert s.fetch_page("https://rival1.test/product/a") == ("<html>rendered</html>", "https://rival1.test/product/a")
    s._page = FakePage(fail=True)
    assert s.fetch_page("https://rival1.test/product/a") is None


def test_browser_scraper_respects_robots(make_client, cfg):
    c = make_client({"/robots.txt": (200, "User-agent: *\nDisallow: /private\n", "text/plain")})
    s = BrowserScraper(c, cfg.competitors[0], cfg)
    s._page = FakePage()
    assert s.fetch_page("https://rival1.test/private/x") is None and s._page.visited == []


def test_browser_scraper_scrape_uses_session(make_client, cfg, monkeypatch):
    from contextlib import contextmanager

    html = '<html><script type="application/ld+json">{"@type":"Product","name":"JS Lamp","sku":"L1","offers":{"price":"500","priceCurrency":"INR","availability":"InStock"}}</script></html>'
    page = FakePage(html)
    c = make_client({"/sitemap.xml": (200, '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>https://rival1.test/product/a</loc></url></urlset>', "application/xml")})
    s = BrowserScraper(c, cfg.competitors[0], cfg)

    @contextmanager
    def fake_session():
        yield page

    monkeypatch.setattr(s, "_session", fake_session)
    recs = s.scrape()
    assert [r.title for r in recs] == ["JS Lamp"] and page.visited == ["https://rival1.test/product/a"]


def test_browser_mode_switch(monkeypatch):
    assert browser_enabled("off") is False
    monkeypatch.delenv("COMPETITOR_INTEL_BROWSER")
    assert browser_enabled("on") is True
    monkeypatch.setenv("COMPETITOR_INTEL_BROWSER", "off")
    assert browser_enabled("on") is False  # env override wins


def test_missing_playwright_gives_actionable_error(make_client, cfg, monkeypatch):
    import builtins

    real = builtins.__import__

    def fake_import(name, *a, **kw):
        if name.startswith("playwright"):
            raise ImportError("no playwright")
        return real(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    s = BrowserScraper(make_client({}), cfg.competitors[0], cfg)
    try:
        s.scrape()
        raise AssertionError("expected ScrapeError")
    except Exception as exc:
        assert "requirements-browser.txt" in str(exc)
    assert SimpleNamespace  # keep import used
