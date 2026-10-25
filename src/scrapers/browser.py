"""Optional headless-browser fallback for sites that render products with JavaScript.

Needs:  pip install -r requirements-browser.txt  &&  playwright install chromium
Reuses the sitemap/JSON-LD logic of JsonLdScraper but fetches pages through Chromium. Respects robots.txt, the
per-domain rate limit and the SSRF guard (every browser request, including sub-resources, is checked)."""
from __future__ import annotations

import importlib.util
import os
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from loguru import logger

from src.scrapers.base import ScrapeError
from src.scrapers.http import UnsafeURLError
from src.scrapers.jsonld import JsonLdScraper

BLOCKED_RESOURCES = {"image", "media", "font"}


def browser_enabled(mode: str) -> bool:
    mode = os.environ.get("COMPETITOR_INTEL_BROWSER", mode)
    if mode == "off":
        return False
    return mode == "on" or importlib.util.find_spec("playwright") is not None


class BrowserScraper(JsonLdScraper):
    method = "browser"

    def __init__(self, *a: Any, **kw: Any) -> None:
        super().__init__(*a, **kw)
        self._page: Any = None

    @contextmanager
    def _session(self) -> Iterator[Any]:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise ScrapeError("browser fallback not installed (pip install -r requirements-browser.txt && playwright install chromium)") from exc
        guard = self.client.url_guard
        with sync_playwright() as pw:
            try:
                browser = pw.chromium.launch()
            except Exception as exc:
                raise ScrapeError(f"could not start Chromium (run `playwright install chromium`): {str(exc).splitlines()[0]}") from exc
            try:
                ctx = browser.new_context(locale="en-IN", user_agent=self.client._headers()["User-Agent"])

                def route(r: Any) -> None:
                    if r.request.resource_type in BLOCKED_RESOURCES:
                        return r.abort()
                    try:
                        if guard:
                            guard(r.request.url)
                    except UnsafeURLError:
                        return r.abort()
                    return r.continue_()

                ctx.route("**/*", route)
                yield ctx.new_page()
            finally:
                browser.close()

    def scrape(self):
        with self._session() as page:
            self._page = page
            try:
                return super().scrape()
            finally:
                self._page = None

    def fetch_page(self, url: str) -> tuple[str, str] | None:
        if self._page is None or not self.client.is_allowed(url):
            return None
        self.client.wait_turn(url)
        try:
            self._page.goto(url, wait_until="domcontentloaded", timeout=25_000)
            try:
                self._page.wait_for_load_state("networkidle", timeout=4_000)
            except Exception:  # noqa: S110 - some pages never go idle; the DOM we have is enough
                pass
            return self._page.content(), self._page.url
        except Exception as exc:
            logger.debug("browser fetch failed for {}: {}", url, exc)
            return None
