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
    mode: str  # allow_all | rules | disallow_all
    parser: RobotFileParser | None = None

    def can_fetch(self, url: str) -> bool:
        if self.mode == "allow_all":
            return True
        if self.mode == "disallow_all" or self.parser is None:
            return False
        return self.parser.can_fetch("*", url)


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

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> PoliteClient:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- robots.txt -------------------------------------------------------
    def _policy(self, origin: str) -> _RobotsPolicy:
        if origin in self._robots:
            return self._robots[origin]
        res = self._fetch(f"{origin}/robots.txt")
        if res is not None and res.status_code == 200:
            rp = RobotFileParser()
            rp.parse(res.text.splitlines())
            policy = _RobotsPolicy("rules", rp)
        elif res is not None and 400 <= res.status_code < 500:
            policy = _RobotsPolicy("allow_all")  # no robots.txt = no restrictions (RFC 9309)
        else:
            logger.warning("robots.txt unreachable for {}; treating site as disallowed this run", origin)
            policy = _RobotsPolicy("disallow_all")
        self._robots[origin] = policy
        return policy

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

    def _throttle(self, host: str) -> None:
        interval = 1.0 / self.settings.rate_limit_per_sec
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

    def _once(self, url: str) -> FetchResult:
        self._throttle(urlparse(url).netloc)
        try:
            resp = self._client.get(url, headers=self._headers())
        except httpx.TransportError as exc:
            raise _RetryableError(f"{type(exc).__name__}: {exc}") from exc
        if resp.status_code == 429 or resp.status_code >= 500:
            raise _RetryableError(f"HTTP {resp.status_code}")
        return FetchResult(
            url=str(resp.url),
            status_code=resp.status_code,
            text=resp.text,
            content_type=resp.headers.get("content-type", ""),
            headers={k.lower(): v for k, v in resp.headers.items()},
        )

    def _fetch(self, url: str) -> FetchResult | None:
        """Request with retries. Returns None if all attempts failed at network/5xx/429 level."""
        result: FetchResult | None = None
        try:
            for attempt in Retrying(
                stop=stop_after_attempt(self.settings.max_retries),
                wait=wait_exponential(multiplier=1, min=1, max=10),
                retry=retry_if_exception_type(_RetryableError),
                sleep=self._sleep,
                reraise=True,
            ):
                with attempt:
                    result = self._once(url)
        except _RetryableError as exc:
            logger.warning("Giving up on {} after {} attempts: {}", url, self.settings.max_retries, exc)
            return None
        except UnsafeURLError as exc:
            logger.warning("Blocked unsafe request to {}: {}", url, exc)
            return None
        return result

    def get(self, url: str, *, params: dict | None = None, use_cache: bool = True) -> FetchResult | None:
        """GET with robots check, cache, throttle and retries. None = blocked/unreachable."""
        full = str(self._client.build_request("GET", url, params=params).url)
        if not self.is_allowed(full):
            logger.warning("Blocked by robots.txt (skipped): {}", full)
            return None
        if use_cache and not self.fresh:
            cached = self._read_cache(full)
            if cached is not None:
                return cached
        res = self._fetch(full)
        if res is not None and res.status_code == 200:
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
