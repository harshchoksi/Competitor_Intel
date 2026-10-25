import socket

import httpx
import pytest

from src.scrapers.http import PoliteClient, UnsafeURLError
from src.web import security
from src.web.security import check_url_is_public, validate_user_url


@pytest.fixture(autouse=True)
def _clear_cache():
    security._host_is_public.cache_clear()
    yield
    security._host_is_public.cache_clear()


def _resolve_to(monkeypatch, ip):
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, port: [(socket.AF_INET, 0, 0, "", (ip, 0))])


def test_scheme_defaults_and_origin(monkeypatch):
    _resolve_to(monkeypatch, "93.184.216.34")
    assert validate_user_url("Shop.example.com/collections/x", allow_private=False) == "https://Shop.example.com"
    assert validate_user_url("localhost:8101", allow_private=True) == "http://localhost:8101"


@pytest.mark.parametrize("bad", ["", "   ", "nodots", "ftp://example.com"])
def test_garbage_rejected(bad, monkeypatch):
    _resolve_to(monkeypatch, "93.184.216.34")
    with pytest.raises(ValueError):
        validate_user_url(bad, allow_private=False)


@pytest.mark.parametrize("ip", ["127.0.0.1", "10.0.0.5", "192.168.1.10", "169.254.169.254", "172.16.0.1", "::1"])
def test_private_and_metadata_addresses_blocked(monkeypatch, ip):
    _resolve_to(monkeypatch, ip)
    with pytest.raises(ValueError, match="private or internal"):
        validate_user_url("evil.example.com", allow_private=False)


def test_unresolvable_host_rejected(monkeypatch):
    def boom(host, port):
        raise socket.gaierror("nope")

    monkeypatch.setattr(socket, "getaddrinfo", boom)
    with pytest.raises(ValueError, match="cannot resolve"):
        validate_user_url("does-not-exist.example", allow_private=False)


def test_public_ip_allowed(monkeypatch):
    _resolve_to(monkeypatch, "93.184.216.34")
    check_url_is_public("https://example.com/x")
    with pytest.raises(UnsafeURLError):
        check_url_is_public("file:///etc/passwd")


def test_redirect_to_internal_address_is_blocked(settings, monkeypatch):
    def fake_getaddrinfo(host, port):
        ip = "169.254.169.254" if host == "169.254.169.254" else "93.184.216.34"
        return [(socket.AF_INET, 0, 0, "", (ip, 0))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    settings.respect_robots = False

    def handler(request):
        if request.url.host == "169.254.169.254":
            return httpx.Response(200, text="SECRETS")
        return httpx.Response(302, headers={"location": "http://169.254.169.254/latest/meta-data/"})

    with PoliteClient(settings, transport=httpx.MockTransport(handler), sleep=lambda s: None, url_guard=check_url_is_public) as c:
        assert c.get("https://shop.example.com/") is None  # redirect hop was refused
