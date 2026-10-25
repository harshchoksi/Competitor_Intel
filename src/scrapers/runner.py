"""Orchestrates detect -> scrape (with fallback chain) -> persist, isolating failures per site."""
from __future__ import annotations

import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

from loguru import logger
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from src.config import AppConfig, ScrapingSettings, SiteConfig
from src.db.models import Offer, PriceSnapshot, Product, Run, ScrapeLog, Site, VariantSnapshot, utcnow
from src.db.session import session_scope
from src.scrapers.base import BaseScraper, ProductRecord, ScrapeError
from src.scrapers.browser import BrowserScraper, browser_enabled
from src.scrapers.detector import DetectionResult, Platform, detect_platform
from src.scrapers.http import PoliteClient
from src.scrapers.jsonld import JsonLdScraper
from src.scrapers.shopify import ShopifyScraper
from src.scrapers.signals import collect_site_signals
from src.scrapers.woocommerce import WooCommerceScraper


@dataclass
class SiteResult:
    site: SiteConfig
    is_source: bool
    platform: str | None = None
    method: str | None = None
    products: list[ProductRecord] = field(default_factory=list)
    status: str = "failed"  # ok | failed
    error: str | None = None
    duration: float = 0.0
    reachable: bool = True
    signals: dict | None = None
    social: dict | None = None


def build_chain(detection: DetectionResult, settings: ScrapingSettings | None = None) -> list[type[BaseScraper]]:
    if detection.platform == Platform.SHOPIFY and detection.feed_available:
        chain: list[type[BaseScraper]] = [ShopifyScraper, JsonLdScraper]
    elif detection.platform == Platform.WOOCOMMERCE and detection.feed_available:
        chain = [WooCommerceScraper, JsonLdScraper]
    else:
        chain = [JsonLdScraper]
    if browser_enabled(settings.use_browser if settings else "auto"):
        chain.append(BrowserScraper)  # slowest option, tried last
    return chain


BROWSER_HINT = " If this site loads its products with JavaScript, install the optional browser (see README, 'JavaScript-rendered sites')."


def scrape_site(site: SiteConfig, is_source: bool, cfg: AppConfig, client: PoliteClient) -> SiteResult:
    """Products via the scraper chain, then (best effort) offers/policies/social/marketing signals."""
    result = _scrape_products(site, is_source, cfg, client)
    if result.reachable:
        result.signals = collect_site_signals(client, site.origin)
    return result


def _scrape_products(site: SiteConfig, is_source: bool, cfg: AppConfig, client: PoliteClient) -> SiteResult:
    t0 = time.monotonic()
    result = SiteResult(site=site, is_source=is_source)
    try:
        detection = detect_platform(client, site.url)
        result.platform = detection.platform.value
        if not detection.reachable:
            result.error, result.reachable = "; ".join(detection.evidence), False
            return result
        logger.info("{}: detected {} ({})", site.name, detection.platform.value, "; ".join(detection.evidence))
        errors: list[str] = []
        chain = build_chain(detection, cfg.scraping)
        for scraper_cls in chain:
            scraper = scraper_cls(client, site, cfg)
            try:
                products = scraper.scrape()
            except ScrapeError as exc:
                errors.append(f"{scraper.method}: {exc}")
                logger.warning("{}: {} failed: {}", site.name, scraper.method, exc)
                continue
            if products:
                result.products, result.method, result.status = products, scraper.method, "ok"
                logger.info("{}: {} products via {}", site.name, len(products), scraper.method)
                return result
            errors.append(f"{scraper.method}: no products returned")
        result.error = "; ".join(errors) or "no scraper produced data"
        if BrowserScraper not in chain:
            result.error += BROWSER_HINT
    except Exception as exc:  # one broken site must never crash the whole run
        logger.exception("{}: unexpected error", site.name)
        result.error = f"unexpected error: {type(exc).__name__}: {exc}"
    finally:
        result.duration = time.monotonic() - t0
    return result


def _get_or_create_site(session: Session, res: SiteResult) -> Site:
    s = session.scalar(select(Site).where(Site.key == res.site.key))
    if s is None:
        s = Site(key=res.site.key, name=res.site.name, url=res.site.url, is_source=res.is_source)
        session.add(s)
    s.url, s.is_source = res.site.url, res.is_source
    s.facebook_page_id, s.instagram_handle, s.youtube_channel = (
        res.site.facebook_page_id, res.site.instagram_handle, res.site.youtube_channel,
    )
    if res.platform:
        s.platform = res.platform
    session.flush()
    return s


def persist_result(session: Session, run_id: int, res: SiteResult) -> None:
    site = _get_or_create_site(session, res)
    now = utcnow()
    session.add(
        ScrapeLog(
            run_id=run_id, site_id=site.id, platform_detected=res.platform, method=res.method,
            product_count=len(res.products), status=res.status, error=res.error, duration_seconds=res.duration,
        )
    )
    for o in (res.signals or {}).get("offers", []):
        session.add(Offer(site_id=site.id, run_id=run_id, type=o["type"], text=o["text"], code=o.get("code"), discount_value=o.get("discount_value"), detected_at=now))
    if res.status != "ok":
        return
    existing = {p.external_id: p for p in session.scalars(select(Product).where(Product.site_id == site.id))}
    for rec in res.products:
        prod = existing.get(rec.external_id)
        if prod is None:
            prod = Product(site_id=site.id, external_id=rec.external_id, first_seen_at=now)
            session.add(prod)
            existing[rec.external_id] = prod
        prod.url, prod.title, prod.brand = rec.url, rec.title, rec.brand
        prod.sku, prod.gtin, prod.mpn = rec.sku, rec.gtin, rec.mpn
        prod.category, prod.image_url, prod.tags = rec.category, rec.image_url, rec.tags
        prod.last_seen_at = now
        session.flush()
        session.add(
            PriceSnapshot(
                product_id=prod.id, run_id=run_id, price=rec.price, compare_at_price=rec.compare_at_price,
                currency=rec.currency, in_stock=rec.in_stock, low_stock_flag=rec.low_stock,
                is_preorder=rec.is_preorder, rating=rec.rating, review_count=rec.review_count, scraped_at=now,
            )
        )
        for v in rec.variants:
            session.add(
                VariantSnapshot(
                    product_id=prod.id, run_id=run_id, variant_external_id=v.external_id, title=v.title,
                    sku=v.sku, price=v.price, compare_at_price=v.compare_at_price, available=v.available,
                )
            )


ProgressFn = Callable[[str, str, "SiteResult | None"], None]  # (event: start|done, site_key, result)


def run_scrape(
    cfg: AppConfig,
    engine: Engine,
    client: PoliteClient,
    only: set[str] | None = None,
    progress: ProgressFn | None = None,
    parallel: int = 1,
) -> tuple[int, list[SiteResult]]:
    """Scrape all configured sites. Different domains may run in parallel; the rate limit is per domain."""
    with session_scope(engine) as s:
        run = Run(status="running")
        s.add(run)
        s.flush()
        run_id = run.id

    targets = [
        (site, is_source)
        for site, is_source in cfg.all_sites()
        if not only or site.key in only or site.name.lower() in only
    ]

    def emit(event: str, key: str, res: SiteResult | None = None) -> None:
        if progress:
            try:
                progress(event, key, res)
            except Exception:
                logger.exception("progress callback failed")

    def finish(res: SiteResult) -> None:
        try:
            with session_scope(engine) as s:
                persist_result(s, run_id, res)
        except Exception as exc:
            logger.exception("{}: could not save results", res.site.name)
            res.status, res.error = "failed", f"database error: {exc}"
        emit("done", res.site.key, res)

    by_key: dict[str, SiteResult] = {}
    if parallel <= 1 or len(targets) <= 1:
        for site, is_source in targets:
            emit("start", site.key)
            res = scrape_site(site, is_source, cfg, client)
            by_key[site.key] = res
            finish(res)
    else:
        with ThreadPoolExecutor(max_workers=min(parallel, len(targets))) as pool:
            futures = {}
            for site, is_source in targets:
                emit("start", site.key)
                futures[pool.submit(scrape_site, site, is_source, cfg, client)] = site
            for fut in as_completed(futures):
                res = fut.result()  # scrape_site never raises
                by_key[res.site.key] = res
                finish(res)
    results = [by_key[site.key] for site, _ in targets]

    ok = sum(r.status == "ok" for r in results)
    status = "success" if results and ok == len(results) else "partial" if ok else "failed"
    notes = "; ".join(f"{r.site.name}: {r.error}" for r in results if r.status != "ok") or None
    with session_scope(engine) as s:
        run = s.get(Run, run_id)
        run.status, run.finished_at, run.notes = status, utcnow(), notes
    return run_id, results
