"""URL safety. A site that fetches user-supplied URLs must not be usable to reach internal networks (SSRF)."""
from __future__ import annotations

import ipaddress
import socket
from functools import lru_cache
from urllib.parse import urlparse

from src.scrapers.http import UnsafeURLError


@lru_cache(maxsize=512)
def _host_is_public(host: str) -> str | None:
    """Return None if every resolved address is public, else a reason string."""
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return f"cannot resolve host '{host}'"
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if not ip.is_global:
            return f"'{host}' points to a private or internal address"
    return None


def check_url_is_public(url: str) -> None:
    """Guard used on every outgoing request (including redirects). Raises UnsafeURLError."""
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        raise UnsafeURLError(f"unsupported URL: {url}")
    reason = _host_is_public(p.hostname)
    if reason:
        raise UnsafeURLError(reason)


def validate_user_url(url: str, *, allow_private: bool) -> str:
    """Normalise a user-typed URL to its origin and reject unsafe targets. Raises ValueError."""
    url = (url or "").strip()
    if not url:
        raise ValueError("URL is empty")
    if not url.startswith(("http://", "https://")):
        bare_host = url.split("/")[0].split(":")[0].lower()
        url = ("http://" if bare_host in ("localhost", "127.0.0.1") else "https://") + url
    p = urlparse(url)
    if not p.hostname or "." not in p.hostname and p.hostname != "localhost":
        raise ValueError(f"'{url}' is not a valid website address")
    if not allow_private:
        try:
            check_url_is_public(url)
        except UnsafeURLError as exc:
            raise ValueError(str(exc)) from exc
    return f"{p.scheme}://{p.netloc}"
