"""Polite HTTP client: robots.txt, per-domain rate limiting, retries with backoff, raw-response cache."""
from __future__ import annotations

import hashlib
import json
import random
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import httpx
from loguru import logger
from tenacity import Retrying, retry_if_exception_type, stop_after_attempt, wait_exponential

from src.config import ScrapingSettings


class _RetryableError(Exception):
    pass


class UnsafeURLError(Exception):
    """Raised by a url_guard when a request (or redirect) targets a disallowed address."""


@dataclass
class FetchResult:
    url: str
    status_code: int
    text: str
    content_type: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    from_cache: bool = False

    @property
    def ok(self) -> bool:
        return self.status_code == 200

    def json(self):
        try:
            return json.loads(self.text)
        except (ValueError, TypeError):
            return None


@dataclass
class _RobotsPolicy:
    # allow_all: no robots.txt (404...) | rules: parsed | disallow_all: kept for completeness
    # unreachable: robots.txt could not be downloaded (timeouts / 429 / 5xx). NOT the same as "the site forbids us": re-checked later.
    mode: str
    parser: RobotFileParser | None = None
    detail: str = ""
    checked_at: float = 0.0

    def can_fetch(self, url: str) -> bool:
        if self.mode == "allow_all":
            return True
        if self.mode in ("disallow_all", "unreachable") or self.parser is None:
            return False
        return self.parser.can_fetch("*", url)


ROBOTS_TRIES = (0, 2, 5, 10)  # seconds to wait before each of up to 4 attempts to download robots.txt (about 17 s in the worst case)
ROBOTS_RECHECK_SECONDS = 30  # an "unreachable" verdict is not final: asked again after this long


class PoliteClient:
    def __init__(
        self,
        settings: ScrapingSettings,
        *,
        transport: httpx.BaseTransport | None = None,
        fresh: bool = False,
        sleep=time.sleep,
        monotonic=time.monotonic,
        url_guard: Callable[[str], None] | None = None,
        proxy_transport: httpx.BaseTransport | None = None,  # tests only: stands in for the real proxy connection
    ) -> None:
        self.settings = settings
        self.fresh = fresh
        self._sleep = sleep
        self._monotonic = monotonic
        self._rng = random.Random()
        self._last_request: dict[str, float] = {}
        self._lock = threading.Lock()
        self._robots: dict[str, _RobotsPolicy] = {}
        self.url_guard = url_guard
        self._cache_dir = Path(settings.cache_dir)
        hooks = {"request": [lambda req: url_guard(str(req.url))]} if url_guard else {}
        self._client = httpx.Client(
            timeout=settings.timeout_seconds, follow_redirects=True, transport=transport, event_hooks=hooks
        )
        self._hooks = hooks
        self._robots_lock = threading.RLock()
        self._proxy_transport = proxy_transport
        self._proxy_client: httpx.Client | None = None
        self.proxy_hosts: set[str] = set()  # sites we only managed to read through the proxy
        self.last_error: dict[str, str] = {}  # host -> the most recent transport/HTTP problem, for readable messages
        self.failures: dict[str, str] = {}  # host -> "robots" | "network": why the last request to it returned nothing

    def close(self) -> None:
        self._client.close()
        if self._proxy_client is not None:
            self._proxy_client.close()

    @property
    def proxy_configured(self) -> bool:
        return bool(self.settings.proxy_url) or self._proxy_transport is not None

    def _proxy(self) -> httpx.Client:
        if self._proxy_client is None:
            self._proxy_client = httpx.Client(timeout=self.settings.timeout_seconds, follow_redirects=True, event_hooks=self._hooks,
                                              proxy=self.settings.proxy_url if self._proxy_transport is None else None, transport=self._proxy_transport)
        return self._proxy_client

    def __enter__(self) -> PoliteClient:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- robots.txt -------------------------------------------------------
    def _policy(self, origin: str) -> _RobotsPolicy:
        with self._robots_lock:
            cached = self._robots.get(origin)
            if cached and not (cached.mode == "unreachable" and self._monotonic() - cached.checked_at > ROBOTS_RECHECK_SECONDS):
                return cached
            detail = "no response"
            policy: _RobotsPolicy | None = None
            for wait in ROBOTS_TRIES:
                if wait:
                    self._sleep(wait)
                res = self._fetch(f"{origin}/robots.txt", attempts=1)
                if res is not None and res.status_code == 200:
                    rp = RobotFileParser()
                    rp.parse(res.text.splitlines())
                    policy = _RobotsPolicy("rules", rp)
                    break
                if res is not None and 400 <= res.status_code < 500 and res.status_code != 429:
                    policy = _RobotsPolicy("allow_all")  # no robots.txt = no restrictions (RFC 9309)
                    break
                detail = f"HTTP {res.status_code}" if res is not None else self.last_error.get(urlparse(origin).netloc, "no response")
            if policy is None:
                logger.warning("robots.txt could not be downloaded for {} ({}); site skipped for now, will re-check", origin, detail)
                policy = _RobotsPolicy("unreachable", detail=detail)
            policy.checked_at = self._monotonic()
            self._robots[origin] = policy
            return policy

    def pause(self, seconds: float) -> None:
        self._sleep(seconds)

    def forget_robots(self, origin: str) -> None:
        """Drop a remembered robots.txt verdict (used to retry a site whose robots.txt was only temporarily unreachable)."""
        with self._robots_lock:
            self._robots.pop(origin, None)

    def is_allowed(self, url: str) -> bool:
        if not self.settings.respect_robots:
            return True
        p = urlparse(url)
        return self._policy(f"{p.scheme}://{p.netloc}").can_fetch(url)

    def site_maps(self, origin: str) -> list[str]:
        policy = self._policy(origin)
        if policy.parser is None:
            return []
        return list(policy.parser.site_maps() or [])

    # -- requests ---------------------------------------------------------
    def _headers(self) -> dict[str, str]:
        return {
            "User-Agent": self._rng.choice(self.settings.user_agents),
            "Accept": "text/html,application/xhtml+xml,application/xml,application/json;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-IN,en;q=0.9",
        }

    def _throttle(self, host: str, rate: float | None = None) -> None:
        interval = 1.0 / (rate or self.settings.rate_limit_per_sec)
        with self._lock:
            now = self._monotonic()
            last = self._last_request.get(host)
            wait = max(0.0, interval - (now - last)) if last is not None else 0.0
            self._last_request[host] = now + wait  # reserve our slot so parallel threads queue politely
        if wait > 0:
            self._sleep(wait)

    def wait_turn(self, url: str) -> None:
        """Respect the per-domain rate limit for requests made outside this client (e.g. a headless browser)."""
        self._throttle(urlparse(url).netloc)

    def _once(self, url: str, client: httpx.Client | None = None, rate: float | None = None) -> FetchResult:
        self._throttle(urlparse(url).netloc, rate)
        try:
            resp = (client or self._client).get(url, headers=self._headers())
        except httpx.TransportError as exc:
            self.last_error[urlparse(url).netloc] = "timeout" if isinstance(exc, httpx.TimeoutException) else f"connection error ({type(exc).__name__})"
            raise _RetryableError(f"{type(exc).__name__}: {exc}") from exc
        if resp.status_code == 429 or resp.status_code >= 500:
            self.last_error[urlparse(url).netloc] = f"HTTP {resp.status_code}"
            retry_after = resp.headers.get("retry-after", "")
            if resp.status_code == 429 and retry_after.isdigit():
                self._sleep(min(int(retry_after), 20))  # the site told us how long to back off; ignoring it gets datacenter IPs blocked
            raise _RetryableError(f"HTTP {resp.status_code}")
        return FetchResult(
            url=str(resp.url),
            status_code=resp.status_code,
            text=resp.text,
            content_type=resp.headers.get("content-type", ""),
            headers={k.lower(): v for k, v in resp.headers.items()},
        )

    BLOCK_CODES = (401, 403, 406, 429, 503)

    def _fetch(self, url: str, rate: float | None = None, attempts: int | None = None) -> FetchResult | None:
        """Direct request with retries; if the site blocked us and a proxy is configured, one more try through the proxy."""
        res = self._fetch_with(url, None, attempts=attempts, rate=rate)
        if self.proxy_configured and (res is None or res.status_code in self.BLOCK_CODES):
            via = self._fetch_with(url, self._proxy(), attempts=2, rate=rate)
            if via is not None and via.status_code < 400:
                host = urlparse(url).netloc
                if host not in self.proxy_hosts:
                    logger.info("{}: blocked directly, reading it through the proxy", host)
                self.proxy_hosts.add(host)
                return via
        return res

    def _fetch_with(self, url: str, client: httpx.Client | None, attempts: int | None = None, rate: float | None = None) -> FetchResult | None:
        """Request with retries. Returns None if all attempts failed at network/5xx/429 level."""
        result: FetchResult | None = None
        try:
            for attempt in Retrying(
                stop=stop_after_attempt(attempts or self.settings.max_retries),
                wait=wait_exponential(multiplier=1, min=1, max=10),
                retry=retry_if_exception_type(_RetryableError),
                sleep=self._sleep,
                reraise=True,
            ):
                with attempt:
                    result = self._once(url, client, rate)
        except _RetryableError as exc:
            logger.warning("Giving up on {} after {} attempts: {}", url, self.settings.max_retries, exc)
            return None
        except UnsafeURLError as exc:
            logger.warning("Blocked unsafe request to {}: {}", url, exc)
            return None
        return result

    def get(self, url: str, *, params: dict | None = None, use_cache: bool = True, rate: float | None = None) -> FetchResult | None:
        """GET with robots check, cache, throttle and retries. None = blocked/unreachable.
        `rate` (requests/second) overrides the default per-site limit; used for JSON product feeds, which are meant for bulk reading."""
        full = str(self._client.build_request("GET", url, params=params).url)
        host = urlparse(full).netloc
        if not self.is_allowed(full):
            pol = self._robots.get(f"{urlparse(full).scheme}://{host}")
            if pol is not None and pol.mode == "unreachable":
                logger.warning("robots.txt could not be downloaded, skipping: {}", full)
                self.failures[host] = f"robots-unreachable:{pol.detail}"
            else:
                logger.warning("Blocked by robots.txt (skipped): {}", full)
                self.failures[host] = "robots"
            return None
        if use_cache and not self.fresh:
            cached = self._read_cache(full)
            if cached is not None:
                return cached
        res = self._fetch(full, rate)
        if res is None:
            self.failures.setdefault(host, "network")
        elif res.status_code == 200:
            self.failures.pop(host, None)
            self._write_cache(full, res)
        return res

    # -- cache ------------------------------------------------------------
    def _cache_path(self, url: str) -> Path:
        digest = hashlib.sha1(url.encode()).hexdigest()
        return self._cache_dir / urlparse(url).netloc.replace(":", "_") / f"{digest}.json"

    def _read_cache(self, url: str) -> FetchResult | None:
        ttl = self.settings.cache_ttl_minutes * 60
        if ttl <= 0:
            return None
        path = self._cache_path(url)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if time.time() - data["fetched_at"] > ttl:
                return None
            return FetchResult(
                url=data["url"], status_code=data["status_code"], text=data["text"],
                content_type=data.get("content_type", ""), headers=data.get("headers", {}), from_cache=True,
            )
        except (OSError, ValueError, KeyError):
            return None

    def _write_cache(self, url: str, res: FetchResult) -> None:
        if self.settings.cache_ttl_minutes <= 0 or len(res.text) > 1_500_000 or res.content_type.lower().startswith(("image/", "video/", "audio/", "application/pdf")):
            return  # nothing would ever read it back (or it is a huge/binary file); do not fill the disk
        path = self._cache_path(url)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps({
                    "url": res.url, "status_code": res.status_code, "text": res.text,
                    "content_type": res.content_type, "headers": res.headers, "fetched_at": time.time(),
                }),
                encoding="utf-8",
            )
        except OSError as exc:
            logger.debug("Cache write failed for {}: {}", url, exc)
