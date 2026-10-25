import smtplib
import time
from datetime import timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from src.alerts.notify import NotifyError, build_digest, send_all, send_email, send_telegram
from src.db.models import Analysis, Monitor, utcnow
from src.db.session import make_engine, session_scope
from src.monitoring import Scheduler, run_due_monitors
from src.scrapers.http import PoliteClient
from src.web.app import create_app
from tests.test_web_api import SITES, shopify, woo

OFFER_HTML = "<body class=woocommerce><div class='announcement-bar'>Flat 15% OFF sitewide this week</div></body>"


class World:
    """Mutable fake internet so a second analysis sees different prices."""

    def __init__(self):
        self.rival = [("Sonex Headphone Model HP-100", "Sonex", "HP-100", 1800, True), ("Boomer SP-200 Speaker", "Boomer", "SP-200", 4200, True)]
        self.home = "<body class=woocommerce>"

    def sites(self):
        return {"mine.test": SITES["mine.test"], "rival1.test": {"/": self.home, "/wp-json/wc/store/v1/products": woo(self.rival)}}

    def factory(self):
        def make(settings):
            def router(request):
                table = self.sites().get(request.url.host)
                body = table.get(request.url.path) if table else None
                return httpx.Response(200, text=body) if body is not None else httpx.Response(404)

            return PoliteClient(settings, transport=httpx.MockTransport(router), sleep=lambda s: None)

        return make


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for k in ("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD", "TELEGRAM_BOT_TOKEN", "APP_BASE_URL"):
        monkeypatch.delenv(k, raising=False)
    world = World()
    engine = make_engine(f"sqlite:///{tmp_path}/m.db")
    app = create_app(engine=engine, client_factory=world.factory(), llm_factory=lambda: None, youtube_factory=lambda: None, allow_private=True, run_inline=True)
    return world, engine, TestClient(app)


def analyse(client):
    aid = client.post("/api/analyses", json={"source_url": "mine.test", "competitor_urls": ["rival1.test"], "use_ai": False}).json()["id"]
    return client.get(f"/api/analyses/{aid}").json()


def test_first_run_has_no_history_second_run_shows_changes(env):
    world, engine, c = env
    first = analyse(c)["result"]
    assert first["changes"]["has_history"] is False and first["price_history"] is None
    world.rival = [("Sonex Headphone Model HP-100", "Sonex", "HP-100", 1500, True), ("Boomer SP-200 Speaker", "Boomer", "SP-200", 4200, False),
                   ("Voltix PB-100 Power bank", "Voltix", "PB-100", 900, True)]
    world.home = OFFER_HTML
    second = analyse(c)["result"]
    ch = second["changes"]["sites"]["rival1-test"]
    assert second["changes"]["has_history"] and ch["price_drop_count"] == 1 and ch["price_drops"][0]["change_pct"] == -16.7
    assert ch["new_count"] == 1 and ch["new_products"] == ["Voltix PB-100 Power bank"] and ch["went_out_of_stock"] == ["Boomer SP-200 Speaker"]
    assert ch["new_offers"] and "15% OFF" in ch["new_offers"][0]
    kinds = {a["type"] for a in second["alerts"]}
    assert {"price_drop", "new_offer"} <= kinds
    drop = next(a for a in second["alerts"] if a["type"] == "price_drop")
    assert drop["level"] == "warning" and "17%" in drop["message"] or "16%" in drop["message"]
    ph = second["price_history"]
    hp = next(p for p in ph["products"] if "HP-100" in p["title"])
    assert len(ph["labels"]) == 2 and hp["series"]["rival1-test"] == [1800.0, 1500.0] and hp["series"]["mine-test"] == [2000.0, 2000.0]
    assert second["change_frequency"]["rival1-test"]["changes"] >= 1


def test_unchanged_site_reports_no_changes(env):
    _, _, c = env
    analyse(c)
    r = analyse(c)["result"]
    ch = r["changes"]["sites"]["rival1-test"]
    assert ch["price_drop_count"] == ch["new_count"] == ch["removed_count"] == 0 and not ch["new_offers"]
    assert not any(a["type"] in ("price_drop", "new_offer", "new_products") for a in r["alerts"])


def test_monitor_lifecycle(env):
    _, engine, c = env
    r = c.post("/api/monitors", json={"source_url": "mine.test", "competitor_urls": ["rival1.test"], "interval_hours": 6, "use_ai": False, "name": "My watch"})
    assert r.status_code == 201
    mid, aid = r.json()["id"], r.json()["analysis_id"]
    assert c.get(f"/api/analyses/{aid}").json()["status"] == "done"
    m = c.get("/api/monitors").json()[0]
    assert m["name"] == "My watch" and m["interval_hours"] == 6 and m["enabled"] and m["analyses"][0]["id"] == aid and m["next_run_at"] > m["last_run_at"]
    assert c.post(f"/api/monitors/{mid}/run").status_code == 202 and len(c.get("/api/monitors").json()[0]["analyses"]) == 2
    assert c.post(f"/api/monitors/{mid}/toggle").json()["enabled"] is False
    assert c.delete(f"/api/monitors/{mid}").json() == {"deleted": mid} and c.get("/api/monitors").json() == []
    assert c.post("/api/monitors/nope/run").status_code == 404 and c.delete("/api/monitors/nope").status_code == 404


@pytest.mark.parametrize("extra", [{"interval_hours": 5}, {"notify_email": "not-an-email"}, {"telegram_chat_id": "hello"}])
def test_monitor_validation(env, extra):
    _, _, c = env
    assert c.post("/api/monitors", json={"source_url": "mine.test", "competitor_urls": ["rival1.test"], **extra}).status_code == 422


def test_run_due_monitors_only_runs_due_enabled_ones(env):
    _, engine, c = env
    ids = [c.post("/api/monitors", json={"source_url": "mine.test", "competitor_urls": ["rival1.test"], "use_ai": False}).json()["id"] for _ in range(3)]
    with session_scope(engine) as s:
        past, future = utcnow() - timedelta(minutes=5), utcnow() + timedelta(hours=5)
        s.get(Monitor, ids[0]).next_run_at = past
        s.get(Monitor, ids[1]).next_run_at, s.get(Monitor, ids[1]).enabled = past, False
        s.get(Monitor, ids[2]).next_run_at = future
    submitted = []
    started = run_due_monitors(engine, submitted.append)
    assert len(started) == 1 == len(submitted)
    with session_scope(engine) as s:
        m = s.get(Monitor, ids[0])
        assert m.next_run_at > utcnow() + timedelta(hours=23) and m.last_analysis_id == started[0]
        assert s.get(Analysis, started[0]).monitor_id == ids[0]
    assert run_due_monitors(engine, submitted.append) == []  # nothing due any more


def test_scheduler_thread_fires_and_stops(env):
    _, engine, c = env
    mid = c.post("/api/monitors", json={"source_url": "mine.test", "competitor_urls": ["rival1.test"], "use_ai": False}).json()["id"]
    with session_scope(engine) as s:
        s.get(Monitor, mid).next_run_at = utcnow() - timedelta(minutes=1)
    seen = []
    sched = Scheduler(engine, seen.append, tick=0.05)
    sched.start()
    for _ in range(60):
        if seen:
            break
        time.sleep(0.05)
    sched.stop()
    assert len(seen) == 1


# ------------------------------------------------------------------ notifications
class FakeSMTP:
    log: list = []

    def __init__(self, host, port, timeout=None):
        FakeSMTP.log.append(("connect", host, port))

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def starttls(self):
        FakeSMTP.log.append(("starttls",))

    def login(self, u, p):
        FakeSMTP.log.append(("login", u))

    def send_message(self, msg):
        FakeSMTP.log.append(("send", msg["To"], msg["Subject"], msg.get_content()))


def test_send_email_via_smtp(monkeypatch):
    FakeSMTP.log = []
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    send_email("me@x.com", "Subj", "Body", {"SMTP_HOST": "smtp.test", "SMTP_USER": "u", "SMTP_PASSWORD": "p"})
    assert FakeSMTP.log[:3] == [("connect", "smtp.test", 587), ("starttls",), ("login", "u")] and FakeSMTP.log[3][1:3] == ("me@x.com", "Subj")
    with pytest.raises(NotifyError, match="not configured"):
        send_email("me@x.com", "s", "b", {})


def test_send_telegram_success_failure_and_no_token_leak():
    seen = {}

    def ok(req):
        seen["url"], seen["body"] = str(req.url), req.content
        return httpx.Response(200, json={"ok": True})

    send_telegram("12345", "hello", {"TELEGRAM_BOT_TOKEN": "TOKEN123"}, httpx.MockTransport(ok))
    assert "botTOKEN123/sendMessage" in seen["url"] and b"12345" in seen["body"]
    with pytest.raises(NotifyError, match="HTTP 401") as e:
        send_telegram("1", "x", {"TELEGRAM_BOT_TOKEN": "TOKEN123"}, httpx.MockTransport(lambda r: httpx.Response(401, json={"ok": False})))
    assert "TOKEN123" not in str(e.value)

    def boom(req):
        raise httpx.ConnectError("x", request=req)

    with pytest.raises(NotifyError) as e2:
        send_telegram("1", "x", {"TELEGRAM_BOT_TOKEN": "TOKEN123"}, httpx.MockTransport(boom))
    assert "TOKEN123" not in str(e2.value)
    with pytest.raises(NotifyError, match="not configured"):
        send_telegram("1", "x", {})


def test_send_all_reports_per_channel(monkeypatch):
    out = send_all("a@b.co", "123", "s", "b", {"TELEGRAM_BOT_TOKEN": "T"}, httpx.MockTransport(lambda r: httpx.Response(200, json={"ok": True})))
    assert [(o["channel"], o["status"]) for o in out] == [("email", "failed"), ("telegram", "sent")]
    assert send_all(None, None, "s", "b") == []


def test_digest_only_reports_new_persistent_alerts():
    u = {"type": "undercut", "level": "warning", "site": "rv", "product": "HP-100", "message": "rv is 10% cheaper on HP-100"}
    d = {"type": "price_drop", "level": "warning", "site": "rv", "product": "HP-100", "message": "rv cut the price"}
    res = {"alerts": [u, d]}
    subject, body = build_digest(res, "me.test", "abc123", set(), "https://app.example/")
    assert "2 competitor update" in subject and "rv is 10% cheaper" in body and body.endswith("https://app.example/#a=abc123")
    _, body2 = build_digest(res, "me.test", "abc123", {"undercut|rv|HP-100"})
    assert "cheaper" not in body2 and "cut the price" in body2  # the repeated undercut is not re-sent, the price drop is
    assert build_digest({"alerts": [u]}, "me.test", "x", {"undercut|rv|HP-100"}) is None


def test_monitor_run_sends_alerts_for_new_findings(env, monkeypatch):
    world, engine, c = env
    FakeSMTP.log = []
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    monkeypatch.setenv("SMTP_HOST", "smtp.test")
    monkeypatch.setenv("APP_BASE_URL", "http://localhost:8000")
    mid = c.post("/api/monitors", json={"source_url": "mine.test", "competitor_urls": ["rival1.test"], "use_ai": False, "notify_email": "owner@shop.in"}).json()["id"]
    first = c.get("/api/monitors").json()[0]["analyses"][0]
    assert [n["status"] for n in first["notifications"]] == ["sent"]  # first run reports the existing undercut
    assert any(e[0] == "send" and e[1] == "owner@shop.in" for e in FakeSMTP.log)
    sent_before = sum(e[0] == "send" for e in FakeSMTP.log)
    c.post(f"/api/monitors/{mid}/run")  # nothing changed -> nothing new to tell the owner
    second = c.get("/api/monitors").json()[0]["analyses"][0]
    assert second["notifications"][0]["status"] == "nothing new" and sum(e[0] == "send" for e in FakeSMTP.log) == sent_before
    world.rival[0] = ("Sonex Headphone Model HP-100", "Sonex", "HP-100", 1500, True)  # competitor cuts the price
    c.post(f"/api/monitors/{mid}/run")
    third = c.get("/api/monitors").json()[0]["analyses"][0]
    assert third["notifications"][0]["status"] == "sent" and sum(e[0] == "send" for e in FakeSMTP.log) == sent_before + 1
    assert "cut the price" in [e for e in FakeSMTP.log if e[0] == "send"][-1][3]


def test_missing_smtp_does_not_break_the_analysis(env, monkeypatch):
    _, _, c = env
    monkeypatch.delenv("SMTP_HOST", raising=False)
    aid = c.post("/api/monitors", json={"source_url": "mine.test", "competitor_urls": ["rival1.test"], "use_ai": False, "notify_email": "o@s.in"}).json()["analysis_id"]
    a = c.get(f"/api/analyses/{aid}").json()
    assert a["status"] == "done" and a["result"]["notifications"][0]["status"] == "failed" and "not configured" in a["result"]["notifications"][0]["error"]


def test_old_database_gets_new_column(tmp_path):
    from sqlalchemy import text

    from src.db.session import init_db

    e = make_engine(f"sqlite:///{tmp_path}/old.db")
    with e.begin() as conn:
        conn.execute(text("CREATE TABLE analyses (id VARCHAR(32) PRIMARY KEY, created_at DATETIME, finished_at DATETIME, status VARCHAR(20), source_url VARCHAR(500), "
                          "competitor_urls JSON, options JSON, progress JSON, result JSON, error TEXT, run_id INTEGER)"))
    init_db(e)
    with session_scope(e) as s:
        assert s.scalars(select(Analysis)).all() == []  # query works only if the monitor_id column exists
    assert shopify and woo  # keep helper imports used
