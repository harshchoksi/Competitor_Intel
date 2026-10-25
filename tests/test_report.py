import io

import pytest
from pypdf import PdfReader

from src.analysis.metrics import analyze, radar_data
from src.analysis.types import SiteData
from src.reporting.charts import build_charts
from src.reporting.pdf_builder import build_pdf, wrap_name
from src.scrapers.base import ProductRecord


def P(title, price, brand="Sonex", compare_at=None, in_stock=True, low=False, rating=None):
    return ProductRecord(external_id=title, title=title, brand=brand, price=price, compare_at_price=compare_at, in_stock=in_stock, low_stock=low, rating=rating)


def site(key, products, source=False, status="ok", name=None):
    return SiteData(key=key, name=name or key, is_source=source, status=status, platform="custom", method="jsonld", products=products)


def catalogue(factor, stock=True, rating=None, discount=False, title_suffix=""):
    base = [("Sonex HP-100 Headphone", 2000, "Sonex"), ("Boomer SP-200 Speaker", 4000, "Boomer"), ("Chronos WT-300 Watch", 3000, "Chronos"),
            ("Voltix PB-400 Power bank", 1000, "Voltix")]
    return [P(t + title_suffix, round(p * factor), brand=b, in_stock=stock if i % 2 else True, rating=rating,
              compare_at=round(p * factor * 1.3) if discount and i == 0 else None, low=(i == 2)) for i, (t, p, b) in enumerate(base)]


@pytest.fixture(scope="module")
def result():
    sites = [site("mine", catalogue(1.0, rating=4.2), source=True, name="mine.test"),
             site("a", catalogue(0.9, stock=False, rating=4.5, discount=True), name="a.test"),
             site("b", catalogue(1.1, rating=3.9), name="b.test")]
    return analyze(sites)


@pytest.fixture(scope="module")
def pdf_bytes(result):
    return build_pdf(result, {"finished_at": result["generated_at"]})


def text_of(pdf: bytes) -> str:
    return "\n".join(p.extract_text() for p in PdfReader(io.BytesIO(pdf)).pages)


def test_result_has_report_inputs(result):
    assert set(result["price_points"]) == {"mine", "a", "b"} and len(result["price_points"]["a"]) == 4
    assert result["discount_points"]["a"] == [23.1] and result["discount_points"]["mine"] == []
    assert result["radar"]["axes"] and result["radar"]["series"]["mine"]
    assert any(r["priority"] == "high" for r in result["recommendations"])  # a.test undercuts by 10%


def test_radar_drops_dimensions_it_cannot_support():
    stats = {"me": {"name": "me", "product_count": 10, "on_sale_pct": 10.0, "in_stock": 5, "out_of_stock": 5, "unknown_stock": 0, "avg_rating": None},
             "c": {"name": "c", "product_count": 20, "on_sale_pct": 30.0, "in_stock": 0, "out_of_stock": 0, "unknown_stock": 20, "avg_rating": 4.0}}
    rows = [{"source": {"price": 100}, "competitors": {"c": {"price": 90}}}]
    rd = radar_data(stats, rows, "me", ["c"])
    assert rd["axes"] == ["Price competitiveness", "Discount intensity", "Catalogue size"]
    assert set(rd["omitted"]) == {"Stock availability", "Customer rating"}  # missing for some site: omitted, not invented
    assert rd["series"]["c"][0] > rd["series"]["me"][0]  # cheaper site scores higher on price


def test_charts_rendered(result):
    charts = build_charts(result)
    assert {"price_bars", "gap_heatmap", "position", "distribution", "discounts", "stock", "coverage", "ratings", "radar"} <= set(charts)
    for c in charts.values():
        assert c.png[:8] == b"\x89PNG\r\n\x1a\n" and c.aspect > 0.2 and c.takeaway


def test_ratings_chart_skipped_without_ratings():
    sites = [site("mine", catalogue(1.0), source=True), site("a", catalogue(0.9))]
    assert "ratings" not in build_charts(analyze(sites))


def test_pdf_structure_and_rupee_glyph(pdf_bytes):
    assert pdf_bytes[:5] == b"%PDF-"
    reader = PdfReader(io.BytesIO(pdf_bytes))
    assert len(reader.pages) >= 8 and len(reader.outline) >= 6
    txt = text_of(pdf_bytes)
    assert "₹" in txt  # built-in PDF fonts would print a black box here
    for expected in ("Executive summary", "Price comparison", "Alerts", "Appendix A", "Data quality", "Suggested actions"):
        assert expected in txt


def test_pdf_survives_markup_in_scraped_text():
    nasty = "Sonex <b>HP-100</b> & <script>alert(1)</script> Headphone"
    sites = [site("mine", [P(nasty, 2000)], source=True, name="x<y>.test"), site("a", [P("Sonex HP-100 Headphone", 1800)], name="a&b.test")]
    pdf = build_pdf(analyze(sites))
    assert "<script>" in text_of(pdf)  # shown literally, never interpreted


def test_pdf_with_single_competitor_and_no_alerts():
    sites = [site("mine", [P("Sonex HP-100 Headphone", 2000)], source=True), site("a", [P("Sonex HP-100 Headphone", 2000)])]
    r = analyze(sites)
    assert r["alerts"] == [] and build_pdf(r)[:5] == b"%PDF-"


def test_pdf_refused_without_comparison():
    r = analyze([site("mine", [], source=True, status="failed"), site("a", [P("a", 1)])])
    with pytest.raises(ValueError, match="No comparison"):
        build_pdf(r)


def test_wrap_name_breaks_domains_sensibly():
    assert wrap_name("localhost:8102") == "localhost:<br/>8102"
    assert wrap_name("shop.example.com", 11) == "shop.<br/>example.com"
    assert "<br/>" not in wrap_name("a.co")


def test_pdf_with_ai_offers_social_and_history_sections():
    def mk(key, source, price, ship, yt=None):
        sig = {"offers": [{"type": "coupon", "text": "Use code SAVE10 for 10% off", "code": "SAVE10", "discount_value": 10.0}], "free_shipping_threshold": ship, "free_shipping_all": False,
               "payment_methods": ["UPI", "EMI"], "social_links": {"youtube": {"handle": "@x", "url": "https://youtube.com/@x"}, "instagram": {"handle": "x", "url": "https://instagram.com/x"}},
               "marketing_stack": {"advertising": ["Meta Pixel"], "analytics": ["Google Tag Manager"]}, "policies": {"return_window_days": 7, "warranty": "1 year", "shipping_fee": 49}}
        return SiteData(key=key, name=f"{key}.test", is_source=source, status="ok", signals=sig, social={"youtube": yt} if yt else None,
                        products=[P("Sonex HP-100 Headphone", price), P("Boomer SP-200 Speaker", price * 2, brand="Boomer")])

    r = analyze([mk("me", True, 2000, 999, {"status": "ok", "followers": 9000, "posts_last_30d": 4, "engagement_rate_pct": 3.2}),
                 mk("a", False, 1800, 499, {"status": "ok", "followers": 15000, "posts_last_30d": 9, "engagement_rate_pct": 2.1}),
                 mk("b", False, 2100, 799, {"status": "error", "error": "HTTP 403 quotaExceeded"})])
    r["ai"] = {"status": "ok", "model": "gemini-test", "executive_summary": "a.test is cheaper on the headphone.", "removed_statements": 1,
               "key_findings": [{"title": "Price gap", "text": "10% gap on HP-100.", "severity": "warning"}], "risks": ["Loss of headphone sales."],
               "per_competitor_notes": {"a.test": "Cheaper overall."}, "recommended_actions": [{"priority": "high", "action": "Review price", "rationale": "Gap is 10%."}]}
    first = r["rows"][0]
    r["changes"] = {"has_history": True, "previous_run_at": "2026-10-20T10:00:00", "sites": {"a": {"new_count": 2, "removed_count": 1, "new_products": ["x"], "removed_products": [], "cap_reached": False,
                    "price_drop_count": 1, "price_increase_count": 0, "price_drops": [{"title": "Sonex HP-100 Headphone", "url": None, "old": 2000, "new": 1800, "change_pct": -10.0}],
                    "price_increases": [], "went_out_of_stock": [], "back_in_stock": [], "new_offers": ["Flat 15% off"], "previous_run_at": "2026-10-20T10:00:00"}}}
    r["price_history"] = {"labels": ["20 Oct 10:00", "24 Oct 10:00"], "products": [{"title": first["source"]["title"], "series": {"me": [2000.0, 2000.0], "a": [2000.0, 1800.0], "b": [2100.0, None]}}]}
    r["change_frequency"] = {"me": {"changes": 0, "observations": 4, "pct": 0.0}, "a": {"changes": 2, "observations": 4, "pct": 50.0}}
    charts = build_charts(r)
    assert {"price_history", "change_frequency", "social"} <= set(charts)
    pdf = build_pdf(r)
    txt = text_of(pdf)
    for expected in ("AI-written summary", "gemini-test", "Offers, shipping and policies", "SAVE10", "Marketing and social", "15,000 subscribers", "quotaExceeded",
                     "Meta Ad Library", "Changes and trends", "Biggest price drops", "Appendix"):
        assert expected in txt, expected
    assert len(PdfReader(io.BytesIO(pdf)).outline) >= 9


def test_pdf_shows_ai_failure_note_and_first_run_message(result):
    r = {**result, "ai": {"status": "failed", "error": "HTTP 401: bad key"}, "changes": {"has_history": False, "sites": {}}}
    txt = text_of(build_pdf(r))
    assert "AI summary was not available" in txt and "bad key" in txt and "No earlier analysis" in txt
