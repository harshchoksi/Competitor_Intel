import httpx

from src.scrapers.http import PoliteClient


def test_robots_disallow_blocks_fetch(make_client):
    c = make_client({"/robots.txt": (200, "User-agent: *\nDisallow: /private\n", "text/plain"), "/private/x": (200, "secret"), "/ok": (200, "fine")})
    assert c.get("https://a.test/private/x") is None
    assert c.get("https://a.test/ok").text == "fine"
    assert not any(r.url.path == "/private/x" for r in c.transport.calls)


def test_missing_robots_allows_all_but_server_error_disallows(make_client):
    assert make_client({"/x": (200, "hi")}).get("https://a.test/x").text == "hi"
    c = make_client({"/robots.txt": (503, "down"), "/x": (200, "hi")})
    assert c.get("https://a.test/x") is None


def test_robots_can_be_disabled(settings):
    settings.respect_robots = False
    t = httpx.MockTransport(lambda r: httpx.Response(200, text="ok"))
    with PoliteClient(settings, transport=t, sleep=lambda s: None) as c:
        assert c.get("https://a.test/private").text == "ok"


def test_retries_then_succeeds(make_client):
    state = {"n": 0}

    def flaky(request):
        state["n"] += 1
        return httpx.Response(500) if state["n"] < 3 else httpx.Response(200, text="finally")

    c = make_client({"/flaky": flaky})
    assert c.get("https://a.test/flaky").text == "finally" and state["n"] == 3


def test_gives_up_after_max_retries_and_returns_none(make_client):
    c = make_client({"/down": (503, "x")})
    assert c.get("https://a.test/down") is None
    assert sum(1 for r in c.transport.calls if r.url.path == "/down") == 3


def test_client_error_is_returned_not_retried(make_client):
    c = make_client({})
    res = c.get("https://a.test/missing")
    assert res.status_code == 404 and not res.ok
    assert sum(1 for r in c.transport.calls if r.url.path == "/missing") == 1


def test_cache_hit_and_fresh_bypass(make_client):
    c = make_client({"/p": (200, "v1")})
    assert not c.get("https://a.test/p").from_cache
    assert c.get("https://a.test/p").from_cache
    fresh = make_client({"/p": (200, "v2")}, fresh=True)
    res = fresh.get("https://a.test/p")
    assert res.text == "v2" and not res.from_cache


def test_throttle_waits_between_requests(settings):
    settings.rate_limit_per_sec = 2.0
    settings.respect_robots = False
    sleeps, now = [], [0.0]
    t = httpx.MockTransport(lambda r: httpx.Response(200, text="ok"))
    with PoliteClient(settings, transport=t, sleep=sleeps.append, monotonic=lambda: now[0]) as c:
        c.get("https://a.test/1", use_cache=False)
        c.get("https://a.test/2", use_cache=False)
    assert sleeps and abs(sleeps[0] - 0.5) < 1e-9
