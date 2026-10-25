import json

from src.scrapers.base import parse_price, valid_compare_at
from src.scrapers.jsonld import extract_products_from_html, looks_like_product_url, map_availability, parse_sitemap
from src.scrapers.shopify import parse_shopify_product
from src.scrapers.woocommerce import parse_woo_product
from tests.conftest import load_fixture


def test_parse_price_formats():
    assert parse_price("1,29,900.00") == 129900.0
    assert parse_price("₹1,299") == 1299.0
    assert parse_price(499) == 499.0
    assert parse_price("0.00") is None and parse_price(None) is None and parse_price("free") is None
    assert valid_compare_at(100, 100) is None and valid_compare_at(150, 100) == 150


def test_shopify_picks_cheapest_available_variant():
    items = json.loads(load_fixture("shopify_products.json"))["products"]
    rec = parse_shopify_product(items[0], "https://s.test", "INR")
    assert rec.price == 1999.0 and rec.compare_at_price == 2499.0  # cheapest *available* variant, not the sold-out 2,199 one
    assert rec.in_stock is True and rec.tags == ["bestseller", "new"]
    assert rec.url == "https://s.test/products/sonex-hp-100" and rec.brand == "Sonex"
    assert len(rec.variants) == 3 and rec.variants[0].available is False


def test_shopify_equal_compare_at_is_not_a_discount_and_all_sold_out():
    items = json.loads(load_fixture("shopify_products.json"))["products"]
    rec = parse_shopify_product(items[1], "https://s.test", "INR")
    assert rec.compare_at_price is None and rec.in_stock is False and rec.price == 4499.0
    assert parse_shopify_product(items[2], "https://s.test", "INR") is None  # no title


def test_woocommerce_minor_units_and_stock():
    items = json.loads(load_fixture("woo_products.json"))
    a = parse_woo_product(items[0], "INR")
    assert a.price == 5299.0 and a.compare_at_price == 5999.0 and a.low_stock is True
    assert a.rating == 4.5 and a.review_count == 120 and a.brand == "Auralis" and a.sku == "HP-300"
    b = parse_woo_product(items[1], "INR")
    assert b.in_stock is False and b.compare_at_price is None and b.rating is None and b.sku is None


def test_jsonld_graph_listprice_and_limited_availability():
    (rec,) = extract_products_from_html(load_fixture("product_jsonld.html"), "https://r.test/p/wt-100", "INR")
    assert rec.price == 2999.0 and rec.compare_at_price == 3999.0
    assert rec.in_stock is True and rec.low_stock is True
    assert rec.gtin == "8901234567890" and rec.brand == "Chronos" and rec.rating == 4.3 and rec.review_count == 88


def test_microdata_supported():
    (rec,) = extract_products_from_html(load_fixture("product_microdata.html"), "https://r.test/p/pb-200", "INR")
    assert rec.title.startswith("Voltix") and rec.price == 1799.0 and rec.in_stock is False and rec.sku == "PB-200"


def test_page_without_product_data_returns_empty():
    assert extract_products_from_html("<html><body>hello</body></html>", "https://r.test/", "INR") == []


def test_availability_mapping_never_guesses():
    assert map_availability("https://schema.org/InStock") == (True, False, False)
    assert map_availability("http://schema.org/PreOrder") == (False, False, True)
    assert map_availability("https://schema.org/SoldOut")[0] is False
    assert map_availability(None)[0] is None and map_availability("SomethingNew")[0] is None


def test_sitemap_parsing_and_url_heuristics():
    kind, locs = parse_sitemap(load_fixture("sitemap_index.xml"))
    assert kind == "index" and len(locs) == 2
    kind, locs = parse_sitemap('<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>https://a/x</loc></url></urlset>')
    assert kind == "urlset" and locs == ["https://a/x"]
    assert parse_sitemap("<<not xml")[0] == "unknown"
    assert looks_like_product_url("https://a.com/products/abc")
    assert not looks_like_product_url("https://a.com/collections/all")
    assert not looks_like_product_url("https://a.com/products/abc.jpg")
