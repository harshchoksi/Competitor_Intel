"""Background analysis pipeline: scrape -> match -> metrics -> store result for the website."""
from __future__ import annotations

import os
import shutil
import threading
import time
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlparse

from loguru import logger
from sqlalchemy import Engine, select

from src.analysis.history import attach_history
from src.analysis.metrics import analyze
from src.analysis.pricing import PricingOptions
from src.analysis.promotions import attach_promo_history
from src.analysis.types import SiteData
from src.config import AppConfig, ScrapingSettings
from src.db.models import Analysis, Site, SocialSnapshot, utcnow
from src.db.session import session_scope
from src.llm.gemini import GeminiClient, client_from_env
from src.llm.insights import generate_ai_insights
from src.llm.match_verify import make_verifier
from src.monitoring import notify_for_monitor
from src.scrapers.http import PoliteClient
from src.scrapers.runner import SiteResult, run_scrape
from src.social.youtube import YouTubeClient
from src.social.youtube import client_from_env as youtube_from_env
from src.web.security import check_url_is_public

ClientFactory = Callable[[ScrapingSettings], PoliteClient]
LLMFactory = Callable[[], GeminiClient | None]
YouTubeFactory = Callable[[], YouTubeClient | None]


def default_llm_factory() -> GeminiClient | None:
    return client_from_env()


def default_client_factory(allow_private: bool) -> ClientFactory:
    def make(settings: ScrapingSettings) -> PoliteClient:
        return PoliteClient(settings, url_guard=None if allow_private else check_url_is_public)

    return make


def site_label(url: str) -> str:
    host = urlparse(url).netloc
    return host[4:] if host.startswith("www.") else host


def is_hosted() -> bool:
    """True on Render / Railway / Fly etc. (they set these variables), or when HOSTED=1. Hosted servers are small and shared,
    so they get tighter time budgets than your own computer."""
    return bool(os.environ.get("RENDER") or os.environ.get("RAILWAY_ENVIRONMENT") or os.environ.get("FLY_APP_NAME") or os.environ.get("HOSTED") == "1")


def site_time_budget_minutes() -> float:
    """Longest we spend reading ONE website before using what we have. Override with SITE_TIME_BUDGET_MINUTES."""
    raw = os.environ.get("SITE_TIME_BUDGET_MINUTES", "").strip()
    try:
        return float(raw) if raw else (10.0 if is_hosted() else 45.0)
    except ValueError:
        return 10.0 if is_hosted() else 45.0


def build_config(source_url: str, competitor_urls: list[str], options: dict, cache_dir: str = "data/cache") -> AppConfig:
    return AppConfig.model_validate({
        "source": {"name": site_label(source_url), "url": source_url},
        "competitors": [{"name": site_label(u), "url": u} for u in competitor_urls],
        "max_products_per_site": options.get("max_products") or None,  # None / 0 = every product
        "match_threshold": options.get("match_threshold", 0.80),
        "categories_to_track": options.get("categories", []),
        **({"currency": options["currency"]} if options.get("currency") else {}),
        "scraping": {"rate_limit_per_sec": options.get("rate_limit_per_sec", 2.0), "cache_ttl_minutes": 10, "cache_dir": cache_dir,
                     "max_site_minutes": site_time_budget_minutes(), "proxy_url": os.environ.get("SCRAPER_PROXY_URL", "").strip() or None},
    })


def pricing_options_from(options: dict) -> PricingOptions:
    d = PricingOptions()
    return PricingOptions(
        tolerance_pct=float(options.get("tolerance_pct", d.tolerance_pct)), max_increase_pct=float(options.get("max_increase_pct", d.max_increase_pct)),
        undercut_pct=float(options.get("undercut_pct", d.undercut_pct)), exclude_out_of_stock=bool(options.get("exclude_out_of_stock", d.exclude_out_of_stock)),
        max_price_ratio=float(options.get("max_price_ratio", d.max_price_ratio)), require_known_size=bool(options.get("require_known_size", d.require_known_size)))


def recover_interrupted_analyses(engine: Engine) -> int:
    """Analyses run in threads inside the web process. When the process dies (out of memory, redeploy, a free plan going to
    sleep) those threads die with it but the database still says 'running', so the page waits forever. At startup nothing of
    ours can still be running, so mark such rows failed with a clear message. Also removes cache folders left behind."""
    with session_scope(engine) as s:
        stuck = list(s.scalars(select(Analysis).where(Analysis.status.in_(("queued", "running")))))
        for a in stuck:
            a.status, a.finished_at = "failed", utcnow()
            a.error = "The server restarted while this analysis was running (this happens when a hosted server runs out of memory, is redeployed or goes to sleep). Please run it again."
    cache = Path("data/cache")
    if cache.is_dir():
        for d in cache.iterdir():
            if d.is_dir() and time.time() - d.stat().st_mtime > 1800:
                shutil.rmtree(d, ignore_errors=True)
    if stuck:
        logger.warning("marked {} interrupted analysis(es) as failed", len(stuck))
    return len(stuck)


def _update(engine: Engine, analysis_id: str, **fields) -> None:
    with session_scope(engine) as s:
        a = s.get(Analysis, analysis_id)
        for k, v in fields.items():
            setattr(a, k, v)


def collect_social(results: list[SiteResult], yt: YouTubeClient | None) -> None:
    """YouTube stats for any competitor/own channel linked from the homepage. Failures are recorded, never raised."""
    if yt is None:
        return
    for r in results:
        handle = ((r.signals or {}).get("social_links") or {}).get("youtube", {}).get("handle")
        if handle:
            r.social = {"youtube": yt.summary(handle)}


def save_social(engine: Engine, run_id: int, results: list[SiteResult]) -> None:
    with session_scope(engine) as s:
        for r in results:
            yt = (r.social or {}).get("youtube")
            site = s.scalar(select(Site).where(Site.key == r.site.key))
            if yt and yt.get("status") == "ok" and site is not None:
                s.add(SocialSnapshot(site_id=site.id, run_id=run_id, platform="youtube", followers=yt.get("followers"),
                                     posts_last_30d=yt.get("posts_last_30d"), engagement_rate=yt.get("engagement_rate_pct")))


def _to_site_data(r: SiteResult) -> SiteData:
    return SiteData(key=r.site.key, name=r.site.name, is_source=r.is_source, status=r.status,
                    platform=r.platform, method=r.method, error=r.error, products=r.products, signals=r.signals, social=r.social)


def execute_analysis(analysis_id: str, engine: Engine, client_factory: ClientFactory, llm_factory: LLMFactory | None = None,
                     youtube_factory: YouTubeFactory | None = None) -> None:
    """Runs one analysis end to end. Never raises: failures are written to the analysis row."""
    try:
        with session_scope(engine) as s:
            a = s.get(Analysis, analysis_id)
            source_url, competitor_urls, options, monitor_id = a.source_url, list(a.competitor_urls), dict(a.options or {}), a.monitor_id
        cache_dir = f"data/cache/{analysis_id}"  # per-analysis: avoids serving stale prices to the next run
        cfg = build_config(source_url, competitor_urls, options, cache_dir)

        progress = {"stage": "scraping", "sites": {
            site.key: {"name": site.name, "role": "source" if is_src else "competitor", "state": "waiting", "products": 0, "message": ""}
            for site, is_src in cfg.all_sites()
        }}
        lock = threading.Lock()
        _update(engine, analysis_id, status="running", progress=progress)

        last_write = [0.0]

        def on_progress(event: str, key: str, res) -> None:  # res: SiteResult for "done", running product count (int) for "count"
            with lock:
                entry = progress["sites"][key]
                if event == "count":
                    entry["products"] = int(res)
                    entry["message"] = f"Reading products... {int(res):,} found so far"
                    now = time.monotonic()
                    if now - last_write[0] < 1.5:  # the page polls every 1.5 s; do not hammer the database
                        return
                    last_write[0] = now
                elif event == "start":
                    entry["state"], entry["message"] = "scraping", "Reading products..."
                    entry["started_at"] = time.time()
                elif res is not None:
                    entry["state"] = "done" if res.status == "ok" else "failed"
                    entry["products"] = len(res.products)
                    entry["message"] = (f"{len(res.products):,} products via {res.platform}" + (f" ({'; '.join(res.warnings)})" if res.warnings else "")
                                        if res.status == "ok" else (res.error or "failed"))
                snapshot = {"stage": progress["stage"], "sites": {k: dict(v) for k, v in progress["sites"].items()}}
            _update(engine, analysis_id, progress=snapshot)

        timings: dict[str, float] = {}
        t_mark = time.monotonic()

        def lap(name: str) -> None:
            nonlocal t_mark
            now = time.monotonic()
            timings[name] = round(now - t_mark, 1)
            t_mark = now

        with client_factory(cfg.scraping) as client:
            run_id, results = run_scrape(cfg, engine, client, progress=on_progress, parallel=len(cfg.all_sites()))
        lap("Reading and saving the websites")

        yt = (youtube_factory or youtube_from_env)()
        try:
            collect_social(results, yt)
            save_social(engine, run_id, results)
        finally:
            if yt is not None:
                yt.close()

        lap("Social media data")
        progress["stage"] = "analyzing"
        _update(engine, analysis_id, run_id=run_id, progress={"stage": "analyzing", "sites": progress["sites"]})
        sites = [_to_site_data(r) for r in results]
        use_ai = options.get("use_ai", True)
        llm = (llm_factory or default_llm_factory)() if use_ai else None
        for r, sd in zip(results, sites, strict=True):  # carry scraper warnings into the data-quality table
            if r.warnings and sd.status == "ok":
                sd.error = "; ".join(r.warnings)
        try:
            result = analyze(sites, match_threshold=cfg.match_threshold, undercut_alert_pct=options.get("undercut_alert_pct", 5.0),
                             currency=options.get("currency") or next((r.currency for r in results if r.is_source and r.currency), None),
                             pricing_options=pricing_options_from(options), verifier=make_verifier(llm) if llm is not None else None)
        except Exception:
            if llm is not None:
                llm.close()
            raise

        lap("Matching products and working out prices" + (" (incl. AI check of borderline matches)" if llm is not None else ""))
        if result["comparison_available"]:
            try:
                attach_history(engine, result, run_id, cfg.product_cap, options.get("undercut_alert_pct", 5.0))
                try:  # how long have today's discounts been running? A failure here must never lose the analysis
                    attach_promo_history(engine, result["promotions"], sites, run_id)
                except Exception:
                    logger.exception("promotion history failed")
            except Exception:
                logger.exception("history analysis failed for {}", analysis_id)
                result["changes"] = {"has_history": False, "sites": {}}
            lap("History and discount tracking")
            progress["stage"] = "writing"
            _update(engine, analysis_id, progress={"stage": "writing", "sites": progress["sites"]})
            result["ai"] = generate_ai_insights(result, llm, use_ai=use_ai)
            lap("AI summary")
        if llm is not None:
            llm.close()

        if monitor_id and result["comparison_available"]:
            try:
                result["notifications"] = notify_for_monitor(engine, analysis_id, monitor_id, result)
            except Exception:
                logger.exception("notification step failed for {}", analysis_id)
                result["notifications"] = [{"channel": "all", "status": "failed", "error": "unexpected error"}]

        timings["Total"] = round(sum(timings.values()), 1)
        result["timings"] = timings
        logger.info("analysis {} finished in {}s: {}", analysis_id, timings["Total"], "; ".join(f"{k} {v}s" for k, v in timings.items() if k != "Total"))
        source_ok = sites[0].status == "ok"
        if not source_ok:
            error = f"Could not read your own website ({sites[0].name}): {sites[0].error or 'unknown error'}"
            _update(engine, analysis_id, status="failed", error=error, result=result, finished_at=utcnow(),
                    progress={"stage": "done", "sites": progress["sites"]})
        elif not result["comparison_available"]:
            error = "None of the competitor websites could be read. See the data quality details."
            _update(engine, analysis_id, status="failed", error=error, result=result, finished_at=utcnow(),
                    progress={"stage": "done", "sites": progress["sites"]})
        else:
            _update(engine, analysis_id, status="done", result=result, finished_at=utcnow(),
                    progress={"stage": "done", "sites": progress["sites"]})
    except Exception as exc:
        logger.exception("analysis {} crashed", analysis_id)
        _update(engine, analysis_id, status="failed", error=f"Unexpected error: {type(exc).__name__}: {exc}", finished_at=utcnow())
    finally:
        shutil.rmtree(f"data/cache/{analysis_id}", ignore_errors=True)
