"""Regression tests for problems that only show up on a hosted server: concurrent analyses, restarts, Postgres limits, memory."""
import os
import subprocess
import sys
import threading
import time

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from src.config import SiteConfig
from src.db.models import Analysis, Base, PriceSnapshot, Product, Run, Site
from src.db.session import init_db, make_engine, normalize_db_url, session_scope
from src.llm.gemini import LLMError
from src.llm.match_verify import make_verifier
from src.scrapers.base import ProductRecord
from src.scrapers.http import PoliteClient
from src.scrapers.jsonld import parse_sitemap
from src.scrapers.runner import SiteResult, persist_result
from src.web.app import create_app
from src.web.jobs import recover_interrupted_analyses

PG = os.environ.get("TEST_DATABASE_URL")  # e.g. postgresql+psycopg://postgres@localhost:5433/ci  (optional, for real-Postgres checks)


def _result(n=5, ext=None, key_url="https://shop.example.com"):
    r = SiteResult(site=SiteConfig(name="shop.example.com", url=key_url), is_source=False, status="ok", platform="shopify", method="shopify")
    r.products = [ProductRecord(external_id=ext or f"id{i}", title=f"Product {i}", price=10.0, in_stock=True) for i in range(n)]
    return r


@pytest.fixture(params=["sqlite"] + (["postgres"] if PG else []))
def engine(request, tmp_path):
    if request.param == "sqlite":
        e = make_engine(f"sqlite:///{tmp_path}/t.db")
    else:
        e = make_engine(PG)
        Base.metadata.drop_all(e)
    init_db(e)
    yield e
    e.dispose()


def _run_id(engine):
    with session_scope(engine) as s:
        r = Run()
        s.add(r)
        s.flush()
        return r.id


def test_duplicate_product_ids_in_one_scrape_do_not_crash(engine):
    res = _result(3)
    res.products.append(res.products[0])
    with session_scope(engine) as s:
        persist_result(s, _run_id(engine), res)
    with session_scope(engine) as s:
        assert len(s.scalars(select(PriceSnapshot)).all()) == 3


def test_overlong_values_are_clipped_instead_of_failing_the_save(engine):
    res = _result(1, ext="https://shop.example.com/products/" + "x" * 400)
    res.products[0].title = "T" * 900
    res.products[0].url = "https://shop.example.com/" + "u" * 1500
    with session_scope(engine) as s:
        persist_result(s, _run_id(engine), res)  # Postgres used to raise "value too long for type character varying(300)"
    with session_scope(engine) as s:
        p = s.scalars(select(Product)).one()
        assert len(p.external_id) == 300 and len(p.title) == 500 and len(p.url) == 1000


def test_second_run_updates_products_and_keeps_history(engine):
    for price in (10.0, 12.0):
        res = _result(4)
        for p in res.products:
            p.price = price
        with session_scope(engine) as s:
            persist_result(s, _run_id(engine), res)
    with session_scope(engine) as s:
        assert len(s.scalars(select(Product)).all()) == 4 and len(s.scalars(select(PriceSnapshot)).all()) == 8


def test_two_analyses_saving_the_same_site_at_once_both_succeed(engine):
    """The 'duplicate key value violates unique constraint sites_key_key' crash: both runs insert the same new site."""
    errors = []

    def one():
        try:
            run_id = _run_id(engine)
            # same code path the real runner uses to save a finished site
            from src.scrapers import runner
            for attempt in (1, 2):
                try:
                    with runner._PERSIST_LOCK, session_scope(engine) as s:
                        persist_result(s, run_id, _result(300))
                    break
                except Exception:
                    if attempt == 2:
                        raise
        except Exception as exc:  # pragma: no cover - the test fails below
            errors.append(repr(exc)[:200])

    threads = [threading.Thread(target=one) for _ in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors, errors
    with session_scope(engine) as s:
        assert len(s.scalars(select(Site)).all()) == 1


def test_normalize_db_url():
    assert normalize_db_url("postgres://u:p@h/db") == "postgresql+psycopg://u:p@h/db"
    assert normalize_db_url("postgresql://u:p@h/db") == "postgresql+psycopg://u:p@h/db"
    assert normalize_db_url("postgresql+psycopg://u@h/db") == "postgresql+psycopg://u@h/db"
    assert normalize_db_url("sqlite:///x.db") == "sqlite:///x.db"


@pytest.mark.skipif(not PG, reason="needs TEST_DATABASE_URL pointing at a Postgres")
def test_several_processes_creating_the_tables_at_once_is_safe():
    e = make_engine(PG)
    Base.metadata.drop_all(e)
    code = "import sys; from src.db.session import make_engine, init_db; init_db(make_engine(sys.argv[1]))"
    procs = [subprocess.Popen([sys.executable, "-c", code, PG], stderr=subprocess.PIPE, text=True, env={**os.environ, "PYTHONPATH": "."}) for _ in range(4)]
    outs = [(p.wait(), p.stderr.read()) for p in procs]
    assert all(rc == 0 for rc, _ in outs), outs  # without the advisory lock: duplicate key ... pg_type_typname_nsp_index


@pytest.mark.skipif(not PG, reason="needs TEST_DATABASE_URL pointing at a Postgres")
def test_id_counters_are_repaired_after_data_was_imported_with_explicit_ids():
    e = make_engine(PG)
    Base.metadata.drop_all(e)
    init_db(e)
    with session_scope(e) as s:
        s.add(Run(id=50))  # explicit id: the serial counter still says 1
    init_db(e)  # startup repairs the counter
    with session_scope(e) as s:
        r = Run()
        s.add(r)
        s.flush()
        assert r.id > 50


# ---------------------------------------------------------------- restarts
def test_analyses_interrupted_by_a_restart_are_marked_failed(tmp_path):
    e = make_engine(f"sqlite:///{tmp_path}/t.db")
    init_db(e)
    with session_scope(e) as s:
        for i, st in enumerate(("running", "queued", "done")):
            s.add(Analysis(id=f"a{i}", status=st, source_url="https://x.test", competitor_urls=["https://y.test"]))
    assert recover_interrupted_analyses(e) == 2
    with session_scope(e) as s:
        got = {a.id: a for a in s.scalars(select(Analysis))}
        assert got["a0"].status == got["a1"].status == "failed" and "restarted" in got["a0"].error and got["a2"].status == "done"


# ---------------------------------------------------------------- crawler
def test_sitemap_image_urls_are_not_treated_as_pages():
    xml = ('<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" xmlns:image="http://www.google.com/schemas/sitemap-image/1.1">'
           '<url><loc>https://s.com/products/a</loc><image:image><image:loc>https://cdn.shopify.com/x/a.jpg?v=1</image:loc></image:image></url>'
           '<url><loc>https://s.com/products/b</loc></url></urlset>')
    assert parse_sitemap(xml) == ("urlset", ["https://s.com/products/a", "https://s.com/products/b"])


def test_page_bodies_and_images_are_not_cached_on_disk(tmp_path):
    from src.config import ScrapingSettings

    def router(request):
        if request.url.path.endswith(".jpg"):
            return httpx.Response(200, content=b"x" * 100, headers={"content-type": "image/jpeg"})
        return httpx.Response(200, text="<html>ok</html>", headers={"content-type": "text/html"})

    c = PoliteClient(ScrapingSettings(cache_dir=str(tmp_path / "c"), cache_ttl_minutes=10, respect_robots=False), transport=httpx.MockTransport(router), sleep=lambda s: None)
    c.get("https://s.test/a.jpg")
    assert not list((tmp_path / "c").rglob("*.json"))
    c.get("https://s.test/page")
    assert len(list((tmp_path / "c").rglob("*.json"))) == 1
    c2 = PoliteClient(ScrapingSettings(cache_dir=str(tmp_path / "d"), cache_ttl_minutes=0, respect_robots=False), transport=httpx.MockTransport(router), sleep=lambda s: None)
    c2.get("https://s.test/page")
    assert not (tmp_path / "d").exists() or not list((tmp_path / "d").rglob("*.json"))


def test_retry_after_is_honoured_but_capped():
    waits = []
    calls = {"n": 0}

    def router(request):
        calls["n"] += 1
        return httpx.Response(429, headers={"retry-after": "9999"}) if calls["n"] == 1 else httpx.Response(200, text="ok")

    from src.config import ScrapingSettings
    c = PoliteClient(ScrapingSettings(respect_robots=False, cache_ttl_minutes=0), transport=httpx.MockTransport(router), sleep=waits.append)
    assert c.get("https://s.test/x").text == "ok" and max(waits) == 20


# ---------------------------------------------------------------- AI step can never stall a run
class _DeadLLM:
    calls = 0

    def generate_json(self, system, prompt):
        _DeadLLM.calls += 1
        raise LLMError("429")


def test_ai_check_gives_up_after_the_first_failure_across_all_competitors():
    from src.analysis.matching import match_products
    from src.analysis.types import SiteData

    def P(t):
        return ProductRecord(external_id=t, title=t, price=1.0)

    me = SiteData(key="me", name="me", is_source=True, status="ok", products=[P("Long Grain Basmati Rice Premium 5kg")])
    rivals = [SiteData(key=f"r{i}", name=f"r{i}", is_source=False, status="ok", products=[P("Basmati Rice 5kg")]) for i in range(5)]
    verify = make_verifier(_DeadLLM())
    for r in rivals:
        ms = match_products(me.products, r.products, 0.97)
        assert verify(ms, me, r) == ms
    assert _DeadLLM.calls == 1  # five competitors, one failed call, then it stays off


def test_ai_check_respects_its_time_budget():
    from src.analysis.matching import match_products
    from src.analysis.types import SiteData

    def P(t):
        return ProductRecord(external_id=t, title=t, price=1.0)

    class Slow:
        calls = 0

        def generate_json(self, system, prompt):
            Slow.calls += 1
            time.sleep(0.05)
            return {"verdicts": []}

    me = SiteData(key="me", name="me", is_source=True, status="ok", products=[P(f"Long Grain Basmati Rice Premium {i}kg") for i in range(1, 60)])
    rv = SiteData(key="r", name="r", is_source=False, status="ok", products=[P(f"Basmati Rice {i}kg") for i in range(1, 60)])
    ms = match_products(me.products, rv.products, 0.99)
    make_verifier(Slow(), batch=5, max_seconds=0.12)(ms, me, rv)
    assert Slow.calls <= 4


# ---------------------------------------------------------------- memory & process behaviour
def test_starting_the_web_app_does_not_load_the_pdf_stack():
    out = subprocess.run([sys.executable, "-c", "import sys, src.web.app; print(any(m in sys.modules for m in ('matplotlib','seaborn','pandas','scipy')))"],
                         capture_output=True, text=True, env={**os.environ, "PYTHONPATH": "."})
    assert out.stdout.strip() == "False", out.stderr


def test_hosted_defaults(monkeypatch):
    from src.web.jobs import build_config, is_hosted, site_time_budget_minutes
    monkeypatch.delenv("RENDER", raising=False)
    monkeypatch.delenv("SITE_TIME_BUDGET_MINUTES", raising=False)
    assert not is_hosted() and site_time_budget_minutes() == 45
    monkeypatch.setenv("RENDER", "true")
    assert is_hosted() and site_time_budget_minutes() == 10
    monkeypatch.setenv("SITE_TIME_BUDGET_MINUTES", "3")
    assert build_config("https://a.test", ["https://b.test"], {}).scraping.max_site_minutes == 3


def test_two_simultaneous_analyses_through_the_real_job_pool(tmp_path, monkeypatch):
    """What happened on the server: a second analysis submitted while the first was running."""
    monkeypatch.chdir(tmp_path)
    shop = lambda n: '{"products":[' + ",".join(f'{{"id":{i + 1},"title":"Item {i}","handle":"h{i}","variants":[{{"id":{i},"price":"5.00","available":true}}]}}' for i in range(n)) + "]}"  # noqa: E731
    home = '<script src="https://cdn.shopify.com/x.js"></script>'

    def factory(settings):
        def router(request):
            if request.url.path == "/":
                return httpx.Response(200, text=home)
            if request.url.path == "/products.json":
                time.sleep(0.05)
                return httpx.Response(200, text=shop(120 if request.url.host == "mine.test" else 100))
            return httpx.Response(404)
        return PoliteClient(settings, transport=httpx.MockTransport(router), sleep=lambda s: None)

    app = create_app(db_url=f"sqlite:///{tmp_path}/t.db", client_factory=factory, allow_private=True, max_workers=2)
    c = TestClient(app)
    ids = [c.post("/api/analyses", json={"source_url": "mine.test", "competitor_urls": ["rival.test"], "use_ai": False}).json()["id"] for _ in range(3)]
    deadline = time.time() + 60
    while time.time() < deadline:
        st = [c.get(f"/api/analyses/{i}?lite=1").json() for i in ids]
        if all(x["status"] in ("done", "failed") for x in st):
            break
        time.sleep(0.3)
    assert [x["status"] for x in st] == ["done"] * 3, [(x["status"], x["error"]) for x in st]
