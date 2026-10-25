
import httpx
from sqlalchemy import func, select

from src.db.models import PriceSnapshot, Product, Run, ScrapeLog, Site
from src.db.session import init_db, make_engine, session_scope
from src.scrapers.detector import Platform, detect_platform
from src.scrapers.jsonld import JsonLdScraper
from src.scrapers.runner import run_scrape
from tests.conftest import load_fixture

SHOPIFY_HOME = '<html><script src="https://cdn.shopify.com/x.js"></script></html>'


def test_detect_shopify_via_feed(make_client):
    c = make_client({"/": (200, SHOPIFY_HOME), "/products.json": (200, load_fixture("shopify_products.json"))})
    d = detect_platform(c, "https://mine.test/some/path")
    assert d.platform == Platform.SHOPIFY and d.feed_available


def test_detect_woocommerce_via_store_api(make_client):
    c = make_client({"/": (200, '<body class="woocommerce">'), "/wp-json/wc/store/v1/products": (200, load_fixture("woo_products.json"))})
    d = detect_platform(c, "https://rival1.test")
    assert d.platform == Platform.WOOCOMMERCE and d.feed_available


def test_shopify_marker_without_feed_is_reported_honestly(make_client):
    d = detect_platform(make_client({"/": (200, SHOPIFY_HOME)}), "https://mine.test")
    assert d.platform == Platform.SHOPIFY and not d.feed_available


def test_detect_custom_and_unreachable(make_client):
    assert detect_platform(make_client({"/": (200, "<html>plain</html>")}), "https://x.test").platform == Platform.CUSTOM
    d = detect_platform(make_client({"/": (403, "denied")}), "https://blocked.test")
    assert not d.reachable


def _sitemap(urls):
    body = "".join(f"<url><loc>{u}</loc></url>" for u in urls)
    return f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{body}</urlset>'


def test_jsonld_scraper_via_sitemap_index(make_client, cfg):
    index = load_fixture("sitemap_index.xml")
    routes = {
        "/sitemap.xml": (200, index, "application/xml"),
        "/sitemap-pages.xml": (200, _sitemap(["https://rival2.test/about"]), "application/xml"),
        "/sitemap-products-1.xml": (200, _sitemap(["https://rival2.test/product/a", "https://rival2.test/product/b"]), "application/xml"),
        "/product/a": (200, load_fixture("product_jsonld.html")),
        "/product/b": (200, load_fixture("product_microdata.html")),
    }
    scraper = JsonLdScraper(make_client(routes), cfg.competitors[1], cfg)
    recs = scraper.scrape()
    assert {r.sku for r in recs} == {"WT-100", "PB-200"}  # the /about page in the non-product sitemap was ignored


def test_jsonld_gives_up_quickly_when_pages_have_no_structured_data(make_client, cfg):
    urls = [f"https://rival2.test/product/{i}" for i in range(30)]
    routes = {"/sitemap.xml": (200, _sitemap(urls), "application/xml")}
    routes.update({f"/product/{i}": (200, "<html>no data</html>") for i in range(30)})
    client = make_client(routes)
    try:
        JsonLdScraper(client, cfg.competitors[1], cfg).scrape()
        raise AssertionError("expected ScrapeError")
    except Exception as exc:
        assert "no schema.org Product data" in str(exc)
    assert sum(1 for r in client.transport.calls if r.url.path.startswith("/product/")) == 10  # did not hammer all 30


def test_category_filter(make_client, cfg):
    cfg.categories_to_track = ["speaker"]
    routes = {"/": (200, SHOPIFY_HOME), "/products.json": (200, load_fixture("shopify_products.json"))}
    from src.scrapers.shopify import ShopifyScraper

    recs = ShopifyScraper(make_client(routes), cfg.source, cfg).scrape()
    assert [r.sku for r in recs] == ["SP-200"]


def test_full_run_isolates_failures_and_stores_history(make_client, cfg):
    shop = {"/": (200, SHOPIFY_HOME), "/products.json": (200, load_fixture("shopify_products.json"))}
    woo = {"/": (200, "<body class=woocommerce>"), "/wp-json/wc/store/v1/products": (200, load_fixture("woo_products.json"))}

    def router(request: httpx.Request) -> httpx.Response:
        table = {"mine.test": shop, "rival1.test": woo}.get(request.url.host)
        if table is None:  # rival2 is completely down
            return httpx.Response(503)
        for key in (request.url.raw_path.decode(), request.url.path):
            if key in table:
                status, body = table[key][:2]
                return httpx.Response(status, text=body)
        return httpx.Response(404)

    client = make_client({})
    client._client = httpx.Client(transport=httpx.MockTransport(router), follow_redirects=True)
    engine = make_engine("sqlite:///:memory:")
    init_db(engine)

    run1, results = run_scrape(cfg, engine, client)
    assert [r.status for r in results] == ["ok", "ok", "failed"]  # rival2 failing did not stop the others
    assert "unreachable" in results[2].error or "robots" in results[2].error or "HTTP" in results[2].error

    run2, _ = run_scrape(cfg, engine, client)
    with session_scope(engine) as s:
        assert s.get(Run, run1).status == "partial"
        assert s.scalar(select(func.count()).select_from(Product)) == 4          # 2 shopify (1 filtered: no title) + 2 woo
        assert s.scalar(select(func.count()).select_from(PriceSnapshot)) == 8    # history kept: 4 products x 2 runs
        assert s.scalar(select(func.count()).select_from(Site)) == 3             # failed site still recorded
        failed = s.scalars(select(ScrapeLog).where(ScrapeLog.status == "failed")).all()
        assert len(failed) == 2 and all(f.error for f in failed)
        prod = s.scalar(select(Product).where(Product.sku == "HP-300"))
        assert prod.first_seen_at <= prod.last_seen_at
