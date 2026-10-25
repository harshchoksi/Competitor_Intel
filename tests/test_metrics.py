from src.analysis.metrics import analyze, discount_pct
from src.analysis.types import SiteData
from src.scrapers.base import ProductRecord


def P(title, price, brand="Sonex", compare_at=None, in_stock=True, low=False):
    return ProductRecord(external_id=title, title=title, brand=brand, price=price, compare_at_price=compare_at, in_stock=in_stock, low_stock=low)


def site(key, products, source=False, status="ok", error=None):
    return SiteData(key=key, name=key, is_source=source, status=status, platform="custom", products=products, error=error)


def test_discount_pct():
    assert discount_pct(P("a", 80, compare_at=100)) == 20.0
    assert discount_pct(P("a", 100, compare_at=100)) is None and discount_pct(P("a", 100)) is None


def test_price_gaps_index_and_alerts():
    me = site("me", [P("Sonex HP-100 Headphone", 2000), P("Boomer SP-200 Speaker", 4000, brand="Boomer")], source=True)
    rival = site("rival", [P("Sonex Headphone HP-100", 1800, compare_at=3000), P("Boomer SP-200 Speaker", 4200, brand="Boomer", in_stock=False, low=True)])
    r = analyze([me, rival])
    assert r["comparison_available"] and len(r["rows"]) == 2
    c = r["competitors"][0]
    assert c["matched"] == 2 and c["cheaper"] == 1 and c["costlier"] == 1
    assert abs(c["price_index"] - 97.5) < 0.1  # gaps -10% and +5% -> mean -2.5%
    hp = next(x for x in r["rows"] if "HP-100" in x["source"]["title"])
    assert hp["competitors"]["rival"]["gap_pct"] == -10.0 and hp["competitors"]["rival"]["discount_pct"] == 40.0
    assert hp["cheapest"] == "rival" and hp["source_rank"] == 2
    types = {a["type"] for a in r["alerts"]}
    assert {"undercut", "competitor_out_of_stock", "competitor_low_stock", "big_discount"} <= types
    assert r["alerts"][0]["level"] == "warning"  # most urgent first
    assert r["sites"]["rival"]["on_sale_count"] == 1 and r["sites"]["rival"]["low_stock"] == 1


def test_you_out_of_stock_is_a_risk():
    me = site("me", [P("Sonex HP-100 Headphone", 2000, in_stock=False)], source=True)
    rival = site("rival", [P("Sonex Headphone HP-100", 2000)])
    assert any(a["type"] == "you_out_of_stock" and a["level"] == "risk" for a in analyze([me, rival])["alerts"])


def test_unknown_stock_is_never_treated_as_out_of_stock():
    me = site("me", [P("Sonex HP-100 Headphone", 2000)], source=True)
    rival = site("rival", [P("Sonex Headphone HP-100", 2000, in_stock=None)])
    r = analyze([me, rival])
    assert not any(a["type"] == "competitor_out_of_stock" for a in r["alerts"]) and r["sites"]["rival"]["unknown_stock"] == 1


def test_failed_competitor_is_reported_not_hidden():
    me = site("me", [P("Sonex HP-100 Headphone", 2000)], source=True)
    ok = site("ok", [P("Sonex Headphone HP-100", 1900)])
    bad = site("bad", [], status="failed", error="blocked")
    r = analyze([me, ok, bad])
    assert r["comparison_available"] and [d["status"] for d in r["data_quality"]] == ["ok", "ok", "failed"]
    assert any(i["title"] == "Incomplete data" for i in r["insights"])


def test_no_comparison_when_source_fails():
    r = analyze([site("me", [], source=True, status="failed", error="x"), site("c", [P("a", 1)])])
    assert not r["comparison_available"] and r["rows"] == []


def test_low_confidence_pairs_go_to_review_not_comparison():
    me = site("me", [P("Nova Phone 15 Pro 128GB", 50000, brand="Nova")], source=True)
    rival = site("rival", [P("Nova Phone 15 Pro 256GB", 60000, brand="Nova")])
    r = analyze([me, rival])
    assert r["rows"] == []
