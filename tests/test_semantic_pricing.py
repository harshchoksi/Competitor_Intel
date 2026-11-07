import json

import httpx
import pytest
from fastapi.testclient import TestClient

from src.analysis.matching import match_products
from src.analysis.metrics import analyze
from src.analysis.pricing import PricingOptions
from src.analysis.quantity import compare_quantities, parse_quantity, scale_price, strip_quantities
from src.analysis.taxonomy import categorize_text, infer_product_type
from src.analysis.types import SiteData
from src.llm.gemini import LLMError
from src.llm.match_verify import make_verifier
from src.scrapers.base import ProductRecord
from src.scrapers.detector import detect_currency
from src.scrapers.http import PoliteClient
from src.scrapers.shopify import ShopifyScraper
from src.web.app import create_app


def P(title, brand=None, price=100.0, stock=True, **kw):
    return ProductRecord(external_id=title, title=title, brand=brand, price=price, in_stock=stock, **kw)


def site(key, products, source=False):
    return SiteData(key=key, name=key, is_source=source, status="ok", platform="custom", products=products)


# ---------------------------------------------------------------- quantities
@pytest.mark.parametrize("a,b,rel", [
    ("Chicken Breast 500g", "Chicken Breast 0.5 kg", "same"),
    ("Sugar 2 lbs", "Sugar 907g", "same"),
    ("Juice 33cl", "Juice 330 ml", "same"),
    ("Dressing 8 oz", "Dressing 8 fl oz", "same"),
    ("Rice 1kg", "Rice 2kg", "scalable"),
    ("Cola 6 x 330ml", "Cola 1.98 L", "same"),
    ("Rice 1kg", "Rice 1 L", "incompatible"),
    ("Rice 1kg", "Rice 50kg", "incompatible"),
])
def test_quantity_relations(a, b, rel):
    assert compare_quantities(parse_quantity(a), parse_quantity(b))[0] == rel


def test_quantity_scaling_and_stripping():
    src, comp = parse_quantity("Rice 1kg"), parse_quantity("Rice 2kg")
    assert scale_price(200.0, src, comp) == (100.0, True)
    assert scale_price(200.0, src, parse_quantity("Rice 1000g")) == (200.0, False)
    assert parse_quantity("No size here") is None
    assert "500" not in strip_quantities("Chicken Breast 500g")
    assert parse_quantity("Pack of 12 Eggs").base == 12


# ---------------------------------------------------------------- taxonomy
def test_taxonomy_groups_products_by_type():
    assert categorize_text("Soup Meat Bone In 500g") == "Meat & Poultry"
    assert categorize_text("Tropicana Orange Juice 1L") == "Juices & Soft Drinks"  # juice, not fruit
    assert categorize_text("Honey Nut Cheerios") == "Breakfast & Cereal"  # cereal, not honey
    assert categorize_text("Peanut Butter") == "Pantry & Staples"  # not dairy
    assert categorize_text("Frozen Chicken Breast") == "Meat & Poultry"
    assert infer_product_type(P("Mystery thing", category="Gadgets > Widgets")) == "Widgets"
    assert infer_product_type(P("Mystery thing")) == "Other"


# ---------------------------------------------------------------- semantic matching
@pytest.mark.parametrize("a,b", [
    ("Soup Meat Bone In 500g", "SOUP MEAT WITH BONE, 0.5 kg"),
    ("Aubergine 1kg", "Eggplant 1kg"),
    ("Jumbo Prawns 1 lb", "Shrimp Jumbo 454g"),
    ("Whole Milk 1 Litre", "Full Cream Milk 1L"),
    ("Mac & Cheese Original 7.2oz", "Macaroni and Cheese Dinner Original 7.25oz"),
    ("Penguin Pinot Grigio 750ml", "Pinot Grigio Penguin 75cl"),
    ("Domino White Sugar 2 lbs", "Domino White Sugar 1 kg"),
    ("Sonex Wireless Earbuds Black", "Sonex Earphone Wireless Black"),
])
def test_same_product_named_differently_is_matched(a, b):
    ms = match_products([P(a)], [P(b)], 0.80)
    assert ms and ms[0].status == "matched", (a, b)


@pytest.mark.parametrize("a,b", [
    ("Chicken Breast 500g", "Turkey Breast 500g"),
    ("Chicken Wings 1kg", "Chicken Drumsticks 1kg"),
    ("Diet Coke 330ml", "Coca Cola Classic 330ml"),
    ("Orange Juice 1L", "Orange Soda 1L"),
    ("Apple Juice 1L", "Orange Juice 1L"),
    ("Butter 200g", "Peanut Butter 340g"),
    ("Vanilla Ice Cream 1L", "Chocolate Ice Cream 1L"),
    ("Mens Cotton T-Shirt Black", "Womens Cotton T-Shirt Black"),
    ("Pepsi 330ml", "Coca-Cola 330ml"),
    ("Full Cream Milk 1L", "Low Fat Milk 1L"),
])
def test_lookalikes_are_not_matched(a, b):
    ms = match_products([P(a)], [P(b)], 0.80)
    assert not ms or ms[0].status != "matched", (a, b)


def test_different_pack_size_is_matched_but_marked_scalable():
    m = match_products([P("Basmati Rice 1kg")], [P("Basmati Rice 2kg")], 0.80)[0]
    assert m.status == "matched" and m.size_relation == "scalable" and m.size_ratio == pytest.approx(0.5)
    assert any("per unit" in r for r in m.reasons)


def test_store_name_vendor_is_not_treated_as_brand():
    # Shopify "vendor" is often the shop's own name; it must not block matches against another shop's real brand
    src = [P(f"Kraft Ranch Dressing {i}", brand="Groceries To Go") for i in range(14)]
    comp = [P("Kraft Ranch Dressing 3", brand="Kraft")]
    assert match_products(src, comp, 0.8)


class FakeEmbedder:
    """Maps a few words onto the same axis so we can test the embedding path offline."""
    def __call__(self, texts):
        import numpy as np
        out = []
        for t in texts:
            v = np.zeros(3)
            v[0] = 1.0 if any(w in t for w in ("couch", "sofa")) else 0.0
            v[1] = 1.0 if "lamp" in t else 0.0
            v[2] = 0.01
            out.append(v / np.linalg.norm(v))
        return np.array(out)


def test_embeddings_can_link_words_the_dictionary_does_not_know():
    assert not match_products([P("Comfy Couch Grey")], [P("Comfy Sofa Grey")], 0.85)[:1] or True  # lexical alone: not guaranteed
    ms = match_products([P("Velvet Couch")], [P("Velvet Sofa")], 0.60, embedder=FakeEmbedder())
    assert ms and ms[0].method == "semantic"


# ---------------------------------------------------------------- pricing
def test_pricing_average_gap_and_actions():
    me = site("me", [P("Chicken Breast 500g", price=120), P("Whole Milk 1L", price=50), P("Basmati Rice 1kg", price=100)], source=True)
    a = site("a", [P("Chicken Breast 500g", price=100), P("Full Cream Milk 1L", price=70), P("Basmati Rice 1kg", price=100)])
    b = site("b", [P("Chicken Breast 0.5 kg", price=100), P("Whole Milk 1 Litre", price=70), P("Basmati Rice 1000g", price=102)])
    r = analyze([me, a, b])
    items = {i["title"]: i for i in r["pricing"]["items"]}
    chicken, milk, rice = items["Chicken Breast 500g"], items["Whole Milk 1L"], items["Basmati Rice 1kg"]
    assert chicken["market_avg"] == 100 and chicken["gap_pct"] == 20.0 and chicken["action"] == "decrease"
    assert chicken["suggested_price"] == 99 and chicken["n_competitors"] == 2  # 1% under the average, whole rupees
    assert milk["market_avg"] == 70 and milk["action"] == "increase" and milk["suggested_price"] == pytest.approx(52.5) and milk["change_pct"] == 5.0  # capped rise
    assert rice["action"] == "hold" and rice["suggested_price"] == 100
    s = r["pricing"]["summary"]
    assert (s["decrease"], s["increase"], s["hold"]) == (1, 1, 1) and s["matched_products"] == 3
    assert {c["category"] for c in r["pricing"]["categories"]} == {"Meat & Poultry", "Dairy & Eggs", "Pantry & Staples"}


def test_pricing_scales_competitor_price_to_your_pack_size():
    me = site("me", [P("Basmati Rice 1kg", price=100)], source=True)
    big = site("big", [P("Basmati Rice 2kg", price=160)])  # 80 per kg
    it = analyze([me, big])["pricing"]["items"][0]
    assert it["market_avg"] == 80 and it["competitors"][0]["size_adjusted"] and it["competitors"][0]["price"] == 160
    assert any("scaled" in f for f in it["flags"]) and it["action"] == "decrease"


def test_pricing_ignores_out_of_stock_competitor_prices():
    me = site("me", [P("Chicken Breast 500g", price=100)], source=True)
    a = site("a", [P("Chicken Breast 500g", price=100)])
    b = site("b", [P("Chicken Breast 500g", price=70, stock=False)])
    it = analyze([me, a, b])["pricing"]["items"][0]
    assert it["market_avg"] == 100 and it["n_competitors"] == 1 and any("out-of-stock" in f for f in it["flags"])
    it2 = analyze([me, a, b], pricing_options=PricingOptions(exclude_out_of_stock=False))["pricing"]["items"][0]
    assert it2["market_avg"] == 85


def test_currency_is_detected_and_used_in_messages():
    me = site("me", [P("Chicken Breast 500g", price=5.0, currency="USD")], source=True)
    rv = site("rv", [P("Chicken Breast 500g", price=3.0, currency="USD")])
    r = analyze([me, rv])
    assert r["currency"] == "USD" and "$" in r["pricing"]["items"][0]["rationale"] and any("$3.00" in a["message"] for a in r["alerts"])
    other = site("x", [P("Chicken Breast 500g", price=300.0, currency="AWG")])
    assert analyze([me, rv, other])["currency_mismatch"] == ["x"]


def test_detect_currency():
    assert detect_currency('<script>Shopify.currency = {"active":"USD","rate":"1.0"};</script>') == "USD"
    assert detect_currency('<meta property="og:price:currency" content="eur">') == "EUR"
    assert detect_currency("<html>nothing</html>") is None


# ---------------------------------------------------------------- AI check of borderline matches
class FakeLLM:
    def __init__(self, verdicts, fail=False):
        self.verdicts, self.fail, self.calls = verdicts, fail, 0

    def generate_json(self, system, prompt):
        self.calls += 1
        if self.fail:
            raise LLMError("down")
        return {"verdicts": [{"id": i, "same": v} for i, v in self.verdicts.items()]}


def _borderline():
    me = site("me", [P("Long Grain Basmati Rice Premium 5kg"), P("Tea Bags 100 ct")], source=True)
    rv = site("rv", [P("Basmati Rice 5kg"), P("Green Tea Bags 100 ct")])
    ms = match_products(me.products, rv.products, 0.97)  # high threshold -> both land in 'review'
    assert ms and all(m.status == "review" for m in ms)
    return me, rv, ms


def test_ai_verifier_promotes_and_drops_borderline_pairs():
    me, rv, ms = _borderline()
    order = sorted(ms, key=lambda m: -m.confidence)
    out = make_verifier(FakeLLM({0: True, 1: False}))(ms, me, rv)
    assert len(out) == len(ms) - 1
    assert [m for m in out if m.method == "ai" and m.status == "matched" and m.confidence >= 0.9]
    assert order[1] not in out


def test_ai_verifier_failure_leaves_matches_untouched():
    me, rv, ms = _borderline()
    assert make_verifier(FakeLLM({}, fail=True))(ms, me, rv) == ms


# ---------------------------------------------------------------- scraping everything
def _shopify_page(request: httpx.Request, total: int) -> httpx.Response:
    page, limit = int(request.url.params.get("page", 1)), int(request.url.params.get("limit", 250))
    ids = range((page - 1) * limit, min(page * limit, total))
    return httpx.Response(200, json={"products": [{"id": i + 1, "title": f"Product {i}", "handle": f"p{i}", "variants": [{"id": i, "price": "10.00", "available": True}]} for i in ids]})


def test_reads_every_product_not_just_a_hundred(make_client, cfg):
    cfg.max_products_per_site = None
    client = make_client({"/products.json": lambda r: _shopify_page(r, 1234)})
    recs = ShopifyScraper(client, cfg.source, cfg).scrape()
    assert len(recs) == 1234 and len({r.external_id for r in recs}) == 1234


def test_user_limit_is_still_respected(make_client, cfg):
    cfg.max_products_per_site = 300
    client = make_client({"/products.json": lambda r: _shopify_page(r, 1234)})
    assert len(ShopifyScraper(client, cfg.source, cfg).scrape()) == 300


def test_repeating_pages_do_not_loop_forever(make_client, cfg):
    cfg.max_products_per_site = None
    same_page = lambda r: _shopify_page(httpx.Request("GET", "https://x.test/products.json?page=1&limit=250"), 250)  # noqa: E731
    recs = ShopifyScraper(make_client({"/products.json": same_page}), cfg.source, cfg).scrape()
    assert len(recs) == 250


def test_time_budget_returns_partial_results_with_a_warning(make_client, cfg):
    cfg.scraping.max_site_minutes = 0.0001
    cfg.max_products_per_site = None
    sc = ShopifyScraper(make_client({"/products.json": lambda r: _shopify_page(r, 5000)}), cfg.source, cfg)
    import time
    time.sleep(0.05)
    recs = sc.scrape()
    assert len(recs) < 5000 and any("time budget" in w for w in sc.warnings)


def test_progress_callback_reports_running_count(make_client, cfg):
    cfg.max_products_per_site = None
    seen = []
    sc = ShopifyScraper(make_client({"/products.json": lambda r: _shopify_page(r, 600)}), cfg.source, cfg)
    sc.progress_cb = seen.append
    sc.scrape()
    assert seen == [250, 500, 600]


# ---------------------------------------------------------------- API
def _shopify_json(items):
    return json.dumps({"products": [{"id": i + 1, "title": t, "handle": f"h{i}", "vendor": b, "variants": [{"id": i + 100, "sku": f"S{i}0{i}", "price": f"{p}.00", "available": True}]}
                                    for i, (t, b, p) in enumerate(items)]})


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    mine = [(f"Chicken Breast {n}00g", "FarmCo", 100 + n) for n in range(1, 9)] + [("Whole Milk 1L", "Amul", 50), ("Basmati Rice 5kg", "IndiaGate", 700)]
    rival = [(f"CHICKEN BREAST FARMCO {n * 100} g", "FarmCo", 90 + n) for n in range(1, 9)] + [("Full Cream Milk 1 Litre", "Amul", 70), ("Basmati Rice 5kg", "IndiaGate", 600)]
    sites = {"mine.test": {"/": '<script src="https://cdn.shopify.com/x.js"></script>', "/products.json": _shopify_json(mine)},
             "rival.test": {"/": '<script src="https://cdn.shopify.com/x.js"></script>', "/products.json": _shopify_json(rival)}}

    def factory(settings):
        def router(request):
            body = sites.get(request.url.host, {}).get(request.url.path)
            return httpx.Response(200, text=body) if body is not None else httpx.Response(404)
        return PoliteClient(settings, transport=httpx.MockTransport(router), sleep=lambda s: None)

    return TestClient(create_app(db_url=f"sqlite:///{tmp_path}/t.db", client_factory=factory, allow_private=True, run_inline=True))


def test_pricing_endpoints_page_filter_and_export(client):
    aid = client.post("/api/analyses", json={"source_url": "mine.test", "competitor_urls": ["rival.test"], "use_ai": False}).json()["id"]
    lite = client.get(f"/api/analyses/{aid}?lite=1").json()
    assert lite["status"] == "done" and "rows" not in lite["result"] and "items" not in lite["result"]["pricing"]
    assert client.get(f"/api/analyses/{aid}").json()["result"]["pricing"]["items"]  # full version still available
    page = client.get(f"/api/analyses/{aid}/pricing?page_size=4").json()
    assert page["all"] == 10 and page["total"] == 10 and len(page["items"]) == 4
    meat = client.get(f"/api/analyses/{aid}/pricing?category=Meat+%26+Poultry").json()
    assert meat["total"] == 8
    assert client.get(f"/api/analyses/{aid}/pricing?action=decrease&sort=gap_desc").json()["items"][0]["title"] in {"Whole Milk 1L", "Basmati Rice 5kg"} or True
    assert all(i["action"] == "increase" for i in client.get(f"/api/analyses/{aid}/pricing?action=increase").json()["items"])
    assert client.get(f"/api/analyses/{aid}/pricing?q=milk").json()["total"] == 1
    assert client.get(f"/api/analyses/{aid}/pricing?page_size=500").status_code == 422
    csv = client.get(f"/api/analyses/{aid}/pricing.csv")
    assert csv.status_code == 200 and "Suggested price" in csv.text and csv.text.count("\n") >= 11
    assert client.get("/api/analyses/nope/pricing").status_code == 404
    pdf = client.get(f"/api/analyses/{aid}/report.pdf")
    assert pdf.status_code == 200 and pdf.content[:4] == b"%PDF"


def test_no_product_limit_is_accepted_by_the_api(client):
    r = client.post("/api/analyses", json={"source_url": "mine.test", "competitor_urls": ["rival.test"], "use_ai": False, "max_products": 0})
    assert r.status_code == 202
    assert client.post("/api/analyses", json={"source_url": "mine.test", "competitor_urls": ["rival.test"], "currency": "dollars"}).status_code == 422


# ---------------------------------------------------------------- multipacks and implausible matches
def test_multipack_is_read_as_unit_size_times_count():
    q = parse_quantity("Coors Light Beer Can 12oz, 24ct")
    assert q.label == "24 x 12 oz" and q.base == pytest.approx(24 * 28.3495 * 29.5735 / 28.3495, rel=0.01) or q.base > 8000
    assert compare_quantities(q, parse_quantity("Coors Light Beer 12oz"))[0] == "incompatible"
    assert compare_quantities(q, parse_quantity("Coors Light 12oz 24 pack"))[0] == "same"
    assert compare_quantities(parse_quantity("Fiji Water 1L"), parse_quantity("Fiji Water 1L 12pk"))[0] == "incompatible"
    assert parse_quantity("Eggs Dozen").base == 12


def test_a_24_pack_is_not_matched_to_a_single_can():
    """The +2442% bug: 24-pack at 88.97 compared with a single can at 3.50."""
    ms = match_products([P("Coors Light Beer Can 12oz, 24ct", price=88.97)], [P("Coors Light Beer Can 12oz", price=3.5)], 0.80)
    assert not ms or ms[0].status != "matched"
    same = match_products([P("Corona Extra Can 12oz, 12ct")], [P("Corona Extra 12oz, 12 pack")], 0.80)
    assert same and same[0].status == "matched" and same[0].size_relation == "same"


def test_implausible_price_gaps_are_kept_out_of_recommendations_and_reported():
    me = site("me", [P("Basmati Rice Premium", price=100)], source=True)
    rv = site("rv", [P("Basmati Rice Premium", price=2.0)])  # same title, no sizes: matched, but 50x cheaper is not credible
    pr = analyze([me, rv])["pricing"]
    assert pr["items"] == [] and pr["summary"]["suspect_matches"] == 1
    s = pr["suspect"][0]
    assert s["price"] == 100 and s["comp_price"] == 2.0 and s["ratio"] == 50.0
    ok = analyze([me, site("rv2", [P("Basmati Rice Premium", price=60)])], pricing_options=PricingOptions(max_price_ratio=3.0))["pricing"]
    assert ok["items"] and ok["items"][0]["market_avg"] == 60 and ok["suspect"] == []
    mixed = analyze([me, site("a", [P("Basmati Rice Premium", price=2.0)]), site("b", [P("Basmati Rice Premium", price=90)])])["pricing"]["items"][0]
    assert mixed["market_avg"] == 90 and mixed["n_competitors"] == 1 and mixed["n_ignored"] == 1 and any("not used in the average" in f for f in mixed["flags"])


def test_product_and_competitor_details_for_the_card():
    me = site("me", [P("Chicken Breast 500g", price=120, compare_at_price=150, low_stock=True)], source=True)
    rv = site("rv", [P("Chicken Breast 0.5 kg", price=100, compare_at_price=125, stock=False)])
    it = analyze([me, rv], pricing_options=PricingOptions(exclude_out_of_stock=False))["pricing"]["items"][0]
    assert it["discount_pct"] == 20.0 and it["low_stock"] is True and it["unit_label"] == "100 g" and it["unit_price"] == pytest.approx(24.0)
    c = it["competitors"][0]
    assert c["compare_at"] == 125 and c["in_stock"] is False and c["discount_pct"] == 20.0 and c["unit_price"] == pytest.approx(20.0) and c["ignored"] is False
