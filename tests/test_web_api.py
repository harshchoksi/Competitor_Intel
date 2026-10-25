import json

import httpx
import pytest
from fastapi.testclient import TestClient

from src.scrapers.http import PoliteClient
from src.web.app import create_app


def shopify(products):
    return json.dumps({"products": [{
        "id": i + 1, "title": t, "handle": f"p{i}", "vendor": brand, "product_type": "Audio",
        "variants": [{"id": i + 100, "title": "Default", "sku": sku, "price": f"{price}.00", "compare_at_price": None, "available": True}],
    } for i, (t, brand, sku, price) in enumerate(products)]})


def woo(products):
    return json.dumps([{
        "id": i + 1, "name": t, "sku": sku, "permalink": f"https://rival1.test/p{i}", "on_sale": False, "is_in_stock": stock,
        "low_stock_remaining": None, "average_rating": "0", "review_count": 0, "categories": [], "brands": [{"name": brand}], "images": [],
        "prices": {"price": str(price * 100), "regular_price": str(price * 100), "currency_code": "INR", "currency_minor_unit": 2},
    } for i, (t, brand, sku, price, stock) in enumerate(products)])


SITES = {
    "mine.test": {"/": '<script src="https://cdn.shopify.com/x.js"></script>',
                  "/products.json": shopify([("Sonex HP-100 Headphone", "Sonex", "HP-100", 2000), ("Boomer SP-200 Speaker", "Boomer", "SP-200", 4000)])},
    "rival1.test": {"/": "<body class=woocommerce>",
                    "/wp-json/wc/store/v1/products": woo([("Sonex Headphone Model HP-100", "Sonex", "HP-100", 1800, True),
                                                           ("Boomer SP-200 Speaker", "Boomer", "SP-200", 4200, False)])},
}


def factory(sites):
    def make(settings):
        def router(request: httpx.Request) -> httpx.Response:
            table = sites.get(request.url.host)
            if table is None:
                return httpx.Response(503)
            body = table.get(request.url.path)
            return httpx.Response(200, text=body) if body is not None else httpx.Response(404)

        return PoliteClient(settings, transport=httpx.MockTransport(router), sleep=lambda s: None)

    return make


@pytest.fixture
def make_app(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # keeps the scraper cache out of the repo

    def _make(sites=SITES, **kw):
        app = create_app(db_url=f"sqlite:///{tmp_path}/t.db", client_factory=factory(sites), allow_private=True, run_inline=True, **kw)
        return TestClient(app)

    return _make


def post(client, source="mine.test", comps=("rival1.test",), **extra):
    return client.post("/api/analyses", json={"source_url": source, "competitor_urls": list(comps), **extra})


def test_full_flow(make_app):
    c = make_app()
    res = post(c, comps=["rival1.test", "rival2.test"])  # rival2 is down
    assert res.status_code == 202
    a = c.get(f"/api/analyses/{res.json()['id']}").json()
    assert a["status"] == "done" and a["error"] is None and a["progress"]["stage"] == "done"
    r = a["result"]
    assert len(r["rows"]) == 2 and r["competitors"][0]["cheaper"] == 1
    assert {d["name"]: d["status"] for d in r["data_quality"]} == {"mine.test": "ok", "rival1.test": "ok", "rival2.test": "failed"}
    assert any(a["type"] == "undercut" for a in r["alerts"])
    assert any(i["title"] == "Incomplete data" for i in r["insights"])
    assert c.get("/api/analyses").json()[0]["id"] == a["id"]


def test_source_site_down_gives_clear_failure(make_app):
    c = make_app({"rival1.test": SITES["rival1.test"]})
    res = post(c)
    a = c.get(f"/api/analyses/{res.json()['id']}").json()
    assert a["status"] == "failed" and "your own website" in a["error"]


def test_all_competitors_down(make_app):
    a = make_app({"mine.test": SITES["mine.test"]})
    body = a.get(f"/api/analyses/{post(a).json()['id']}").json()
    assert body["status"] == "failed" and "competitor" in body["error"]


def test_category_option_is_applied(make_app):
    c = make_app()
    a = c.get(f"/api/analyses/{post(c, categories=['speaker']).json()['id']}").json()
    assert a["status"] == "done" and len(a["result"]["rows"]) == 1


@pytest.mark.parametrize("payload,fragment", [
    ({"source_url": "mine.test", "competitor_urls": ["mine.test"]}, "different website"),
    ({"source_url": "not a url", "competitor_urls": ["x.com"]}, "not a valid"),
    ({"source_url": "mine.test", "competitor_urls": ["  "]}, "at least one"),
])
def test_validation_errors(make_app, payload, fragment):
    r = make_app().post("/api/analyses", json=payload)
    assert r.status_code == 422 and fragment in r.json()["detail"]


def test_limits_and_unknown_id(make_app):
    c = make_app()
    assert c.post("/api/analyses", json={"source_url": "a.com", "competitor_urls": [f"c{i}.com" for i in range(6)]}).status_code == 422
    assert c.post("/api/analyses", json={"source_url": "a.com", "competitor_urls": []}).status_code == 422
    assert c.get("/api/analyses/nope").status_code == 404


def test_private_addresses_rejected_in_production_mode(tmp_path):
    app = create_app(db_url=f"sqlite:///{tmp_path}/p.db", allow_private=False, run_inline=True)
    c = TestClient(app)
    r = c.post("/api/analyses", json={"source_url": "localhost:8101", "competitor_urls": ["127.0.0.1:9"]})
    assert r.status_code == 422 and "private or internal" in r.json()["detail"]


def test_site_serves_frontend(make_app):
    c = make_app()
    assert "Competitor Price Intelligence" in c.get("/").text
    assert c.get("/static/app.js").status_code == 200


def test_pdf_download(make_app):
    c = make_app()
    aid = post(c).json()["id"]
    res = c.get(f"/api/analyses/{aid}/report.pdf")
    assert res.status_code == 200 and res.headers["content-type"] == "application/pdf"
    assert f"competitor-report-{aid}.pdf" in res.headers["content-disposition"] and res.content[:5] == b"%PDF-"


def test_pdf_not_available_cases(make_app):
    c = make_app({"rival1.test": SITES["rival1.test"]})  # source down -> failed analysis
    failed = post(c).json()["id"]
    assert c.get(f"/api/analyses/{failed}/report.pdf").status_code == 409
    assert c.get("/api/analyses/unknown/report.pdf").status_code == 404


def test_pdf_409_while_running(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    import src.web.app as web_app

    started = []
    monkeypatch.setattr(web_app, "execute_analysis", lambda *a, **k: started.append(a))  # job never completes
    app = create_app(db_url=f"sqlite:///{tmp_path}/r.db", client_factory=factory(SITES), allow_private=True, run_inline=True)
    c = TestClient(app)
    aid = post(c).json()["id"]
    assert started and c.get(f"/api/analyses/{aid}").json()["status"] == "queued"
    assert c.get(f"/api/analyses/{aid}/report.pdf").status_code == 409


def test_ai_summary_attached_and_failure_does_not_break_analysis(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from src.llm.gemini import GeminiClient

    good = {"executive_summary": "rival1.test is 10% cheaper on Sonex HP-100 Headphone.", "key_findings": [{"title": "Gap", "text": "The gap is 10%.", "severity": "warning"}]}
    ok_client = lambda: GeminiClient("k", transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": json.dumps(good)}]}}]})))  # noqa: E731
    app = create_app(db_url=f"sqlite:///{tmp_path}/a.db", client_factory=factory(SITES), llm_factory=ok_client, allow_private=True, run_inline=True)
    c = TestClient(app)
    assert c.get("/api/health").json()["ai_available"] is True
    a = c.get(f"/api/analyses/{post(c).json()['id']}").json()
    assert a["status"] == "done" and a["result"]["ai"]["status"] == "ok" and a["result"]["ai"]["key_findings"][0]["title"] == "Gap"
    off = c.get(f"/api/analyses/{post(c, use_ai=False).json()['id']}").json()
    assert off["result"]["ai"]["status"] == "off"

    broken = lambda: GeminiClient("k", transport=httpx.MockTransport(lambda r: httpx.Response(401, json={"error": {"message": "bad key"}})))  # noqa: E731
    app2 = create_app(db_url=f"sqlite:///{tmp_path}/b.db", client_factory=factory(SITES), llm_factory=broken, allow_private=True, run_inline=True)
    b = TestClient(app2).get(f"/api/analyses/{post(TestClient(app2)).json()['id']}").json()
    assert b["status"] == "done" and b["result"]["ai"]["status"] == "failed" and "bad key" in b["result"]["ai"]["error"] and b["result"]["insights"]


def test_no_api_key_means_unavailable(make_app, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    c = make_app()
    assert c.get("/api/health").json()["ai_available"] is False
    assert c.get(f"/api/analyses/{post(c).json()['id']}").json()["result"]["ai"]["status"] == "unavailable"
