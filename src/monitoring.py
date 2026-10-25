"""Saved monitors: re-run a comparison on a schedule and alert on what changed."""
from __future__ import annotations

import os
import threading
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta

from loguru import logger
from sqlalchemy import Engine, select

from src.alerts.notify import alert_key, build_digest, send_all
from src.db.models import Analysis, Monitor, utcnow
from src.db.session import session_scope


def create_analysis(engine: Engine, source_url: str, competitor_urls: list[str], options: dict, monitor_id: str | None = None) -> str:
    aid = uuid.uuid4().hex[:12]
    with session_scope(engine) as s:
        s.add(Analysis(id=aid, source_url=source_url, competitor_urls=competitor_urls, options=options, status="queued",
                       progress={"stage": "queued", "sites": {}}, monitor_id=monitor_id))
    return aid


def start_monitor_run(engine: Engine, monitor_id: str, now: datetime | None = None) -> str | None:
    """Create the next analysis for a monitor and schedule the one after it. Returns the analysis id."""
    now = now or utcnow()
    with session_scope(engine) as s:
        m = s.get(Monitor, monitor_id)
        if m is None:
            return None
        aid = create_analysis(engine, m.source_url, list(m.competitor_urls), dict(m.options or {}), m.id)
        m.last_run_at, m.next_run_at, m.last_analysis_id = now, now + timedelta(hours=m.interval_hours), aid
    return aid


def run_due_monitors(engine: Engine, submit: Callable[[str], object], now: datetime | None = None) -> list[str]:
    now = now or utcnow()
    with session_scope(engine) as s:
        due = [m.id for m in s.scalars(select(Monitor).where(Monitor.enabled.is_(True), Monitor.next_run_at <= now))]
    started = []
    for mid in due:
        aid = start_monitor_run(engine, mid, now)
        if aid:
            started.append(aid)
            submit(aid)
    return started


class Scheduler:
    """Checks for due monitors every `tick` seconds in a background thread (runs only while the website is running)."""

    def __init__(self, engine: Engine, submit: Callable[[str], object], tick: float = 30.0) -> None:
        self.engine, self.submit, self.tick = engine, submit, tick
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="monitor-scheduler")

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.wait(self.tick):
            try:
                run_due_monitors(self.engine, self.submit)
            except Exception:
                logger.exception("scheduler tick failed")


def notify_for_monitor(engine: Engine, analysis_id: str, monitor_id: str, result: dict, env=None, transport=None) -> list[dict]:
    """Send a digest of NEW findings to the monitor's channels. Returns per-channel outcomes (stored in the result)."""
    with session_scope(engine) as s:
        m = s.get(Monitor, monitor_id)
        if m is None or not (m.notify_email or m.telegram_chat_id):
            return []
        email, chat = m.notify_email, m.telegram_chat_id
        prev = s.scalars(select(Analysis).where(Analysis.monitor_id == monitor_id, Analysis.id != analysis_id, Analysis.status == "done")
                         .order_by(Analysis.created_at.desc()).limit(1)).first()
        previous_keys = {alert_key(a) for a in ((prev.result or {}).get("alerts", []) if prev else [])}
    name = result["sites"][result["source_key"]]["name"]
    digest = build_digest(result, name, analysis_id, previous_keys, (env or os.environ).get("APP_BASE_URL"))
    if digest is None:
        return [{"channel": "all", "status": "nothing new", "error": None}]
    return send_all(email, chat, digest[0], digest[1], env, transport)
