"""FastAPI app: JSON API + the single-page website."""
from __future__ import annotations

import re
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from loguru import logger
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import Engine, select

from src.db.models import Analysis, Monitor, utcnow
from src.db.session import init_db, make_engine, session_scope
from src.monitoring import Scheduler, create_analysis, start_monitor_run
from src.reporting.pdf_builder import build_pdf
from src.social.youtube import client_from_env as youtube_from_env
from src.web.jobs import (
    ClientFactory,
    LLMFactory,
    YouTubeFactory,
    default_client_factory,
    default_llm_factory,
    execute_analysis,
    site_label,
)
from src.web.security import validate_user_url

STATIC = Path(__file__).parent / "static"


EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
CHAT_RE = re.compile(r"^(-?\d{3,20}|@\w{3,})$")


class AnalysisRequest(BaseModel):
    source_url: str
    competitor_urls: list[str] = Field(min_length=1, max_length=5)
    max_products: int = Field(default=100, ge=10, le=500)
    categories: list[str] = Field(default_factory=list, max_length=10)
    use_ai: bool = True


class MonitorRequest(AnalysisRequest):
    name: str | None = Field(default=None, max_length=120)
    interval_hours: Literal[1, 6, 12, 24, 168] = 24
    notify_email: str | None = None
    telegram_chat_id: str | None = None

    @field_validator("notify_email")
    @classmethod
    def _email(cls, v):
        v = (v or "").strip()
        if v and not EMAIL_RE.match(v):
            raise ValueError("that does not look like an email address")
        return v or None

    @field_validator("telegram_chat_id")
    @classmethod
    def _chat(cls, v):
        v = (v or "").strip()
        if v and not CHAT_RE.match(v):
            raise ValueError("Telegram chat id should be a number (or @channelname)")
        return v or None


def create_app(
    engine: Engine | None = None,
    *,
    db_url: str = "sqlite:///data/competitor_intel.db",
    client_factory: ClientFactory | None = None,
    llm_factory: LLMFactory | None = None,
    youtube_factory: YouTubeFactory | None = None,
    allow_private: bool = False,
    run_inline: bool = False,
    max_workers: int = 2,
    scheduler: bool = False,
    scheduler_tick: float = 30.0,
    demo: bool = False,
) -> FastAPI:
    engine = engine or make_engine(db_url)
    init_db(engine)
    factory = client_factory or default_client_factory(allow_private)
    llm = llm_factory or default_llm_factory
    yt = youtube_factory or youtube_from_env
    pool = ThreadPoolExecutor(max_workers=max_workers)
    def submit(aid: str) -> None:
        if run_inline:
            execute_analysis(aid, engine, factory, llm, yt)
        else:
            pool.submit(execute_analysis, aid, engine, factory, llm, yt)

    sched = Scheduler(engine, submit, scheduler_tick) if scheduler else None

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if sched:
            sched.start()
        yield
        if sched:
            sched.stop()

    app = FastAPI(title="Competitor Price Intelligence", docs_url="/api/docs", redoc_url=None, lifespan=lifespan)

    @app.get("/api/health")
    def health() -> dict:
        return {"ok": True, "allow_private_urls": allow_private, "ai_available": llm() is not None, "youtube_available": yt() is not None, "demo": demo}

    def clean(req: AnalysisRequest) -> tuple[str, list[str], dict]:
        try:
            source = validate_user_url(req.source_url, allow_private=allow_private)
            comps = [validate_user_url(u, allow_private=allow_private) for u in req.competitor_urls if u.strip()]
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        if not comps:
            raise HTTPException(422, "Add at least one competitor URL")
        labels = [site_label(u) for u in [source, *comps]]
        if len(set(labels)) != len(labels):
            raise HTTPException(422, "Each URL must be a different website (duplicates found)")
        cats = [c.strip() for c in req.categories if c.strip()]
        return source, comps, {"max_products": req.max_products, "categories": cats, "use_ai": req.use_ai}

    @app.post("/api/analyses", status_code=202)
    def create_analysis_endpoint(req: AnalysisRequest) -> dict:
        source, comps, options = clean(req)
        analysis_id = create_analysis(engine, source, comps, options)
        submit(analysis_id)
        return {"id": analysis_id}

    @app.post("/api/monitors", status_code=201)
    def create_monitor(req: MonitorRequest) -> dict:
        source, comps, options = clean(req)
        mid = uuid.uuid4().hex[:12]
        now = utcnow()
        with session_scope(engine) as s:
            s.add(Monitor(id=mid, name=(req.name or site_label(source)).strip(), source_url=source, competitor_urls=comps, options=options,
                          interval_hours=req.interval_hours, enabled=True, notify_email=req.notify_email, telegram_chat_id=req.telegram_chat_id, next_run_at=now))
        aid = start_monitor_run(engine, mid, now)  # first run starts immediately
        submit(aid)
        return {"id": mid, "analysis_id": aid}

    @app.get("/api/monitors")
    def list_monitors() -> list[dict]:
        with session_scope(engine) as s:
            out = []
            for m in s.scalars(select(Monitor).order_by(Monitor.created_at.desc())):
                runs = s.scalars(select(Analysis).where(Analysis.monitor_id == m.id).order_by(Analysis.created_at.desc()).limit(5)).all()
                out.append({"id": m.id, "name": m.name, "source_url": m.source_url, "competitor_count": len(m.competitor_urls), "interval_hours": m.interval_hours,
                            "enabled": m.enabled, "notify_email": m.notify_email, "telegram": bool(m.telegram_chat_id),
                            "last_run_at": m.last_run_at.isoformat() if m.last_run_at else None, "next_run_at": m.next_run_at.isoformat() if m.next_run_at else None,
                            "analyses": [{"id": a.id, "status": a.status, "created_at": a.created_at.isoformat(),
                                          "alerts": len((a.result or {}).get("alerts", [])), "notifications": (a.result or {}).get("notifications", [])} for a in runs]})
            return out

    def _monitor_or_404(s, monitor_id: str) -> Monitor:
        m = s.get(Monitor, monitor_id)
        if m is None:
            raise HTTPException(404, "Monitor not found")
        return m

    @app.delete("/api/monitors/{monitor_id}")
    def delete_monitor(monitor_id: str) -> dict:
        with session_scope(engine) as s:
            s.delete(_monitor_or_404(s, monitor_id))
        return {"deleted": monitor_id}

    @app.post("/api/monitors/{monitor_id}/toggle")
    def toggle_monitor(monitor_id: str) -> dict:
        with session_scope(engine) as s:
            m = _monitor_or_404(s, monitor_id)
            m.enabled = not m.enabled
            if m.enabled:
                m.next_run_at = utcnow()
            return {"id": m.id, "enabled": m.enabled}

    @app.post("/api/monitors/{monitor_id}/run", status_code=202)
    def run_monitor(monitor_id: str) -> dict:
        with session_scope(engine) as s:
            _monitor_or_404(s, monitor_id)
        aid = start_monitor_run(engine, monitor_id)
        submit(aid)
        return {"analysis_id": aid}

    @app.get("/api/analyses/{analysis_id}")
    def get_analysis(analysis_id: str) -> dict:
        with session_scope(engine) as s:
            a = s.get(Analysis, analysis_id)
            if a is None:
                raise HTTPException(404, "Analysis not found")
            return {"id": a.id, "status": a.status, "error": a.error, "progress": a.progress, "result": a.result,
                    "source_url": a.source_url, "competitor_urls": a.competitor_urls,
                    "created_at": a.created_at.isoformat(), "finished_at": a.finished_at.isoformat() if a.finished_at else None}

    @app.get("/api/analyses/{analysis_id}/report.pdf")
    def download_report(analysis_id: str) -> Response:
        with session_scope(engine) as s:
            a = s.get(Analysis, analysis_id)
            if a is None:
                raise HTTPException(404, "Analysis not found")
            if a.status in ("queued", "running"):
                raise HTTPException(409, "The analysis is still running. Try again when it has finished.")
            result = a.result
            meta = {"source_url": a.source_url, "competitor_urls": a.competitor_urls, "analysis_id": a.id,
                    "created_at": a.created_at.isoformat(), "finished_at": a.finished_at.isoformat() if a.finished_at else None}
        if not result or not result.get("comparison_available"):
            raise HTTPException(409, "No report is available because the comparison could not be completed.")
        try:
            pdf = build_pdf(result, meta)
        except Exception as exc:
            logger.exception("report generation failed for {}", analysis_id)
            raise HTTPException(500, "Could not generate the report. See the server log.") from exc
        return Response(pdf, media_type="application/pdf",
                        headers={"Content-Disposition": f'attachment; filename="competitor-report-{analysis_id}.pdf"'})

    @app.get("/api/analyses")
    def list_analyses() -> list[dict]:
        with session_scope(engine) as s:
            rows = s.scalars(select(Analysis).order_by(Analysis.created_at.desc()).limit(10)).all()
            return [{"id": a.id, "status": a.status, "source_url": a.source_url, "competitor_count": len(a.competitor_urls),
                     "created_at": a.created_at.isoformat()} for a in rows]

    if demo:
        @app.post("/api/demo/bump")
        def demo_bump() -> dict:
            from src.demo.mock_sites import bump

            return {"epoch": bump()}

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app
