"""Background analysis pipeline: scrape -> match -> metrics -> store result for the website."""
from __future__ import annotations

import shutil
import threading
from collections.abc import Callable
from urllib.parse import urlparse

from loguru import logger
from sqlalchemy import Engine, select

from src.analysis.history import attach_history
from src.analysis.metrics import analyze
from src.analysis.types import SiteData
from src.config import AppConfig, ScrapingSettings
from src.db.models import Analysis, Site, SocialSnapshot, utcnow
from src.db.session import session_scope
from src.llm.gemini import GeminiClient, client_from_env
from src.llm.insights import generate_ai_insights
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


def build_config(source_url: str, competitor_urls: list[str], options: dict, cache_dir: str = "data/cache") -> AppConfig:
    return AppConfig.model_validate({
        "source": {"name": site_label(source_url), "url": source_url},
        "competitors": [{"name": site_label(u), "url": u} for u in competitor_urls],
        "max_products_per_site": options.get("max_products", 100),
        "categories_to_track": options.get("categories", []),
        "scraping": {"rate_limit_per_sec": options.get("rate_limit_per_sec", 1.0), "cache_ttl_minutes": 10, "cache_dir": cache_dir},
    })


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

        def on_progress(event: str, key: str, res: SiteResult | None) -> None:
            with lock:
                entry = progress["sites"][key]
                if event == "start":
                    entry["state"], entry["message"] = "scraping", "Reading products..."
                elif res is not None:
                    entry["state"] = "done" if res.status == "ok" else "failed"
                    entry["products"] = len(res.products)
                    entry["message"] = (f"{len(res.products)} products via {res.platform}" if res.status == "ok" else (res.error or "failed"))
                snapshot = {"stage": progress["stage"], "sites": {k: dict(v) for k, v in progress["sites"].items()}}
            _update(engine, analysis_id, progress=snapshot)

        with client_factory(cfg.scraping) as client:
            run_id, results = run_scrape(cfg, engine, client, progress=on_progress, parallel=len(cfg.all_sites()))

        yt = (youtube_factory or youtube_from_env)()
        try:
            collect_social(results, yt)
            save_social(engine, run_id, results)
        finally:
            if yt is not None:
                yt.close()

        progress["stage"] = "analyzing"
        _update(engine, analysis_id, run_id=run_id, progress={"stage": "analyzing", "sites": progress["sites"]})
        sites = [_to_site_data(r) for r in results]
        result = analyze(sites, match_threshold=cfg.match_threshold, undercut_alert_pct=options.get("undercut_alert_pct", 5.0))

        if result["comparison_available"]:
            try:
                attach_history(engine, result, run_id, cfg.max_products_per_site, options.get("undercut_alert_pct", 5.0))
            except Exception:
                logger.exception("history analysis failed for {}", analysis_id)
                result["changes"] = {"has_history": False, "sites": {}}
            progress["stage"] = "writing"
            _update(engine, analysis_id, progress={"stage": "writing", "sites": progress["sites"]})
            llm = (llm_factory or default_llm_factory)() if options.get("use_ai", True) else None
            result["ai"] = generate_ai_insights(result, llm, use_ai=options.get("use_ai", True))
            if llm is not None:
                llm.close()

        if monitor_id and result["comparison_available"]:
            try:
                result["notifications"] = notify_for_monitor(engine, analysis_id, monitor_id, result)
            except Exception:
                logger.exception("notification step failed for {}", analysis_id)
                result["notifications"] = [{"channel": "all", "status": "failed", "error": "unexpected error"}]

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
