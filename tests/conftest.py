import json
from pathlib import Path

import httpx
import pytest

from src.config import AppConfig, ScrapingSettings
from src.scrapers.http import PoliteClient

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def make_transport(routes: dict):
    """routes: path(+query) or path -> (status, body[, content_type]) | callable(request)->httpx.Response.
    Unknown paths return 404. Every request is recorded in transport.calls."""
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        for key in (request.url.raw_path.decode(), request.url.path):
            if key in routes:
                r = routes[key]
                if callable(r):
                    return r(request)
                status, body, *ct = r
                if not isinstance(body, str):
                    body = json.dumps(body)
                ctype = ct[0] if ct else "application/json" if body[:1] in "[{" else "text/html"
                return httpx.Response(status, text=body, headers={"content-type": ctype})
        return httpx.Response(404, text="not found")

    t = httpx.MockTransport(handler)
    t.calls = calls  # type: ignore[attr-defined]
    return t


@pytest.fixture
def settings(tmp_path):
    return ScrapingSettings(cache_dir=str(tmp_path / "cache"), rate_limit_per_sec=50, max_retries=3)


@pytest.fixture
def make_client(settings):
    clients = []

    def _make(routes: dict, **kw):
        transport = make_transport(routes)
        c = PoliteClient(settings, transport=transport, sleep=lambda s: None, **kw)
        c.transport = transport  # type: ignore[attr-defined]
        clients.append(c)
        return c

    yield _make
    for c in clients:
        c.close()


@pytest.fixture
def cfg():
    return AppConfig.model_validate({
        "source": {"name": "Mine", "url": "https://mine.test"},
        "competitors": [{"name": "Rival One", "url": "https://rival1.test"}, {"name": "Rival Two", "url": "https://rival2.test"}],
        "max_products_per_site": 50,
    })


@pytest.fixture(autouse=True)
def _no_real_browser(monkeypatch):
    """Tests never launch Chromium; the browser fallback is exercised with fakes."""
    monkeypatch.setenv("COMPETITOR_INTEL_BROWSER", "off")
