from datetime import date, timedelta

import httpx
import pytest
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient
from sqlalchemy import select

from src.analysis.metrics import analyze
from src.analysis.promotions import (
    attach_promo_history,
    build_promotions,
    parse_banner,
    parse_period,
    parse_promotion,
    site_discounts,
)
from src.analysis.types import SiteData
from src.db.models import Run, utcnow
from src.db.session import init_db, make_engine, session_scope
from src.scrapers.base import ProductRecord
from src.scrapers.http import PoliteClient
from src.scrapers.runner import SiteResult, persist_result
from src.scrapers.signals import extract_announcements, extract_signals
from src.web.app import create_app

TODAY = date(2026, 11, 3)  # a Tuesday


# ---------------------------------------------------------------- text parsing
@pytest.mark.parametrize("text,kind,amount,applies,start,end", [
    ("Flat 20% off on all meat | Valid till 15 Nov", "percent_off", 20.0, "Meat & Poultry", None, "2026-11-15"),
    ("Up to 50% OFF on Dairy! Offer ends 30th November 2026", "percent_off", 50.0, "Dairy & Eggs", None, "2026-11-30"),
    ("Mega Sale 5 Nov to 9 Nov: up to 40% off", "percent_off", 40.0, None, "2026-11-05", "2026-11-09"),
    ("Get 15% off frozen foods from 1st-10th December", "percent_off", 15.0, "Frozen Foods", "2026-12-01", "2026-12-10"),
    ("Use code FRESH10 for extra 10% off · 1-15 Nov", "percent_off", 10.0, None, "2026-11-01", "2026-11-15"),
    ("Buy 1 Get 1 Free on drinks this weekend", "bogo", 50.0, "Juices & Soft Drinks", None, "2026-11-08"),
    ("Weekend deal: 2 for 1 on bakery. Today only!", "bogo", 50.0, "Bakery", None, "2026-11-03"),
    ("Black Friday Sale: Save $5 off orders over $50 - ends Sunday", "flat_off", 5.0, None, None, "2026-11-08"),
    ("Sale on 12/11 only - 25% off everything", "percent_off", 25.0, "Everything", "2026-11-12", "2026-11-12"),
])
def test_parse_promotion(text, kind, amount, applies, start, end):
    p = parse_promotion(text, TODAY)
    assert (p["kind"], p["amount"], p["applies_to"], p["start"], p["end"]) == (kind, amount, applies, start, end)


def test_status_days_left_and_code_conditions():
    p = parse_promotion("Flat 20% off on all meat | Valid till 15 Nov", TODAY)
    assert p["status"] == "active" and p["days_left"] == 12
    assert parse_promotion("Sale ended 1 Oct, 30% off", TODAY)["status"] in ("expired", "ongoing")
    up = parse_promotion("Get 15% off from 1st to 10th December", TODAY)
    assert up["status"] == "upcoming" and up["days_left"] == 28
    c = parse_promotion("Subscribe & get 10% off your first order. Use code WELCOME10", TODAY)
    assert c["code"] == "WELCOME10" and "first order" in c["conditions"] and c["status"] == "ongoing"
    f = parse_promotion("Free delivery on orders above ₹499", TODAY)
    assert f["kind"] == "free_shipping" and f["min_spend"] == 499


@pytest.mark.parametrize("text", [
    "Big sale! Price $12.10 now, 1-2 days delivery",  # prices and delivery times are not dates
    "Save 10-15% on selected items",  # a percentage range is not 15 October
    "Valid over 15 Nov, 10% off",  # "over 15" is not a minimum spend
])
def test_no_false_dates_or_spend(text):
    p = parse_promotion(text, TODAY)
    assert p is None or (p["min_spend"] is None and p["start"] is None and (p["end"] in (None, "2026-11-15")))


def test_not_a_promotion():
    assert parse_promotion("Welcome to our store, we sell fresh food", TODAY) is None
    assert parse_promotion("100% natural ingredients", TODAY) is None


def test_period_year_rollover_and_relative_words():
    assert parse_period("ends 5 Jan", date(2026, 12, 20))["end"] == "2027-01-05"
    assert parse_period("ends tonight", TODAY)["end"] == "2026-11-03"
    assert parse_period("this weekend only", TODAY)["end"] == "2026-11-08"
    assert parse_period("7-day sale", TODAY)["duration_days"] == 7
    assert parse_period("Black Friday sale", TODAY)["event"] == "Black Friday"


def test_multi_offer_banner_is_split_and_dates_are_shared():
    got = parse_banner("Buy 1 Get 1 free on selected drinks this weekend | Extra 10% off sitewide with code FRESH10, ends 17 Nov | Flash sale: extra 5% off with code FLASH1, today only!", TODAY, "INR")
    assert [(p["kind"], p["code"], p["end"]) for p in got] == [("bogo", None, "2026-11-08"), ("percent_off", "FRESH10", "2026-11-17"), ("percent_off", "FLASH1", "2026-11-03")]
    assert got[1]["applies_to"] == "Everything" and got[0]["applies_to"] == "Juices & Soft Drinks"
    shared = parse_banner("Flat 20% off on all meat | Valid till 15 Nov", TODAY, "INR")
    assert len(shared) == 1 and shared[0]["end"] == "2026-11-15"


# ---------------------------------------------------------------- banner extraction from HTML
HOME = """<html><head><meta property="og:description" content="Fresh groceries delivered"></head><body>
<div class="announcement-bar">MEAT FESTIVAL: Flat 22% off all meat! Valid from 31 Oct to 12 Nov. Use code MEAT22</div>
<div class="product-card"><span class="badge">20% off</span></div>
<section class="hero-banner"><h2>Dairy Week: up to 18% off all dairy, ends 7 Nov</h2></section>
<p>We are a family store.</p></body></html>"""


def test_announcements_are_found_but_product_badges_are_not():
    ann = extract_announcements(BeautifulSoup(HOME, "lxml"))
    assert any("MEAT22" in a for a in ann) and any("Dairy Week" in a for a in ann)
    assert not any(a.strip().lower() == "20% off" for a in ann)
    assert "announcements" in extract_signals(HOME, "https://s.test")


# ---------------------------------------------------------------- product discounts by type
def P(title, price, was=None, **kw):
    return ProductRecord(external_id=title, title=title, price=price, compare_at_price=was, **kw)


def site(key, products, source=False, signals=None):
    return SiteData(key=key, name=key, is_source=source, status="ok", platform="x", products=products, signals=signals)


def test_discount_depth_is_measured_per_product_type():
    sd = site("a", [P("Chicken Breast 500g", 80, 100), P("Beef Mince 500g", 90, 100), P("Chicken Thighs 1kg", 100), P("Whole Milk 1L", 45, 50), P("Rice 5kg", 500, 50000)])
    d = site_discounts(sd, "INR")
    assert d["on_sale"] == 3 and d["publishes_original_price"]  # a 99% "was" price is treated as a data error
    meat = next(t for t in d["by_type"] if t["type"] == "Meat & Poultry")
    assert meat["products"] == 3 and meat["on_sale"] == 2 and meat["avg_discount_pct"] == 15.0 and meat["max_discount_pct"] == 20.0
    assert d["top_deals"][0]["title"].startswith("Chicken Breast") and d["top_deals"][0]["pct"] == 20.0
    assert next(b for b in d["bands"] if b["label"] == "10-20%")["count"] == 2


def test_site_without_original_prices_is_flagged():
    d = site_discounts(site("a", [P("Chicken Breast 500g", 80)]), "INR")
    assert d["publishes_original_price"] is False and d["on_sale"] == 0


def test_build_promotions_compares_you_with_competitors():
    me = site("me", [P("Chicken Breast 500g", 95, 100), P("Beef Mince 500g", 100)] + [P(f"Whole Milk {i}L", 50) for i in range(1, 4)], source=True,
              signals={"offers": [], "announcements": ["Free shipping on orders above ₹999"]})
    rival = site("rv", [P(f"Chicken Cut {i} 500g", 70, 100) for i in range(4)] + [P("Whole Milk 1L", 50)],
                 signals={"offers": [], "announcements": ["Meat Festival: flat 30% off all meat, ends 15 Nov"]})
    pr = build_promotions([me, rival], "me", "INR", TODAY)
    meat = next(r for r in pr["types"] if r["type"] == "Meat & Poultry")
    assert meat["yours"] == 5.0 and meat["best_competitor_avg"] == 30.0 and meat["gap_pts"] == 25.0 and meat["competitors_on_sale"] == 4
    assert any(c["site_key"] == "rv" and c["amount"] == 30.0 and c["applies_to"] == "Meat & Poultry" and c["days_left"] == 12 for c in pr["campaigns"])
    assert any("Deeper discounts on Meat & Poultry" == i["title"] for i in pr["insights"]) and pr["recommendations"]
    assert pr["summary"]["deepest"]["key"] == "rv" and pr["summary"]["active_campaigns"] >= 1


def test_analyze_includes_promotions_and_merges_headlines():
    me = site("me", [P("Chicken Breast 500g", 120), P("Whole Milk 1L", 50)], source=True)
    rv = site("rv", [P("Chicken Breast 500g", 80, 100), P("Whole Milk 1L", 45, 50)])
    r = analyze([me, rv])
    assert r["promotions"]["sites"]["rv"]["on_sale"] == 2 and r["promotions"]["source_key"] == "me"
    assert any(i["title"] == "Competitor discounting" for i in r["insights"])


# ---------------------------------------------------------------- duration measured over repeated runs
def test_how_long_discounts_have_run_is_measured_from_history(tmp_path):
    e = make_engine(f"sqlite:///{tmp_path}/t.db")
    init_db(e)
    from src.config import SiteConfig

    def run(days_ago: int, on_sale: bool, banner: str):
        with session_scope(e) as s:
            r = Run(started_at=utcnow() - timedelta(days=days_ago))
            s.add(r)
            s.flush()
            rid = r.id
        prods = [P(f"Chicken Cut {i} 500g", 80 if on_sale else 100, 100) for i in range(4)]
        res = SiteResult(site=SiteConfig(name="rv", url="https://rv.test"), is_source=False, status="ok", platform="x", method="x")
        res.products, res.signals = prods, {"offers": [], "announcements": [banner]}
        with session_scope(e) as s:
            persist_result(s, rid, res)
        return rid, prods

    run(10, False, "Welcome")
    run(6, True, "Meat Festival: flat 20% off meat")
    run(3, True, "Meat Festival: flat 20% off meat")
    rid, prods = run(0, True, "Meat Festival: flat 20% off meat, ends 15 Nov")
    sd = site("rv", prods, signals={"offers": [], "announcements": ["Meat Festival: flat 20% off meat, ends 15 Nov"]})
    pr = build_promotions([sd], "me", "INR", TODAY)
    attach_promo_history(e, pr, [sd], rid)
    h = pr["history"]["rv"]
    assert h["median_days_on_sale"] == pytest.approx(6.0, abs=0.2) and h["new_discounts_this_run"] == 0 and h["runs_observed"] == 4
    c = pr["campaigns"][0]
    assert c["runs_seen"] == 3 and c["seen_days"] == pytest.approx(6.0, abs=0.2)
    assert any("How long discounts run" == i["title"] for i in pr["insights"])
    with session_scope(e) as s:  # banner rows must not be reported as "new offers" by the older change tracking
        from src.analysis.history import _offers
        from src.db.models import Site
        assert _offers(s, s.scalar(select(Site.id)), rid) == {}


# ---------------------------------------------------------------- API + PDF
@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    home = '<script src="https://cdn.shopify.com/x.js"></script><div class="announcement-bar">Flat 25% off all meat, valid till 30 Nov. Use code MEAT25</div>'

    def feed(host):
        sale = host == "rival.test"
        items = [(f"Chicken Breast {n}00g", 100 + n, (130 + n) if sale else None) for n in range(1, 7)] + [("Whole Milk 1L", 50, None)]
        import json
        return json.dumps({"products": [{"id": i + 1, "title": t, "handle": f"h{i}", "vendor": "FarmCo", "variants": [{"id": i + 50, "sku": f"S{i}0{i}", "price": f"{p}.00",
                                                                                                                  "compare_at_price": f"{w}.00" if w else None, "available": True}]} for i, (t, p, w) in enumerate(items)]})

    def factory(settings):
        def router(request):
            if request.url.path == "/":
                return httpx.Response(200, text=home)
            if request.url.path == "/products.json":
                return httpx.Response(200, text=feed(request.url.host))
            return httpx.Response(404)
        return PoliteClient(settings, transport=httpx.MockTransport(router), sleep=lambda s: None)

    return TestClient(create_app(db_url=f"sqlite:///{tmp_path}/t.db", client_factory=factory, allow_private=True, run_inline=True))


def test_api_exposes_promotions_csv_and_pdf(client):
    aid = client.post("/api/analyses", json={"source_url": "mine.test", "competitor_urls": ["rival.test"], "use_ai": False}).json()["id"]
    r = client.get(f"/api/analyses/{aid}?lite=1").json()
    assert r["status"] == "done", r["error"]
    pr = r["result"]["promotions"]
    assert pr["sites"]["rival-test"]["on_sale"] == 6 and pr["sites"]["rival-test"]["avg_discount_pct"] > 15
    assert any(c["amount"] == 25.0 and c["code"] == "MEAT25" and c["applies_to"] == "Meat & Poultry" for c in pr["campaigns"])
    csv = client.get(f"/api/analyses/{aid}/promotions.csv")
    assert csv.status_code == 200 and "CAMPAIGNS AND BANNER OFFERS" in csv.text and "Meat & Poultry" in csv.text
    assert client.get("/api/analyses/nope/promotions.csv").status_code == 404
    pdf = client.get(f"/api/analyses/{aid}/report.pdf")
    assert pdf.status_code == 200 and pdf.content[:4] == b"%PDF"
