"""FastAPI web dashboard for resbot status monitoring."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from sse_starlette.sse import EventSourceResponse

from resbot.config import load_profile, load_targets
from resbot.models import BookingResult, ReservationTarget, TargetStatus
from resbot.scheduler import ReservationScheduler

logger = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).parent / "templates"


def _render_dashboard(targets, statuses) -> str:
    """Render dashboard HTML directly — no Jinja2 TemplateResponse needed."""
    with open(TEMPLATES_DIR / "dashboard_base.html") as f:
        base = f.read()

    if not targets:
        cards_html = '<div class="empty"><p>No targets configured. Use <code>python3 run.py target add</code> to create one.</p></div>'
    else:
        cards = []
        for t in targets:
            status = statuses.get(t.id)

            # Badge
            if status and status.completed and status.last_result and status.last_result.success:
                badge = '<span class="badge badge-success">BOOKED</span>'
            elif not t.enabled:
                badge = '<span class="badge badge-disabled">DISABLED</span>'
            elif status and status.completed:
                badge = '<span class="badge badge-failed">MAX RETRIES</span>'
            else:
                badge = '<span class="badge badge-active">ACTIVE</span>'

            window = t.effective_window
            window_str = f"{window.earliest.strftime('%H:%M')} - {window.latest.strftime('%H:%M')}"

            # Status rows
            status_rows = ""
            if status:
                last_try = status.last_attempt.strftime('%Y-%m-%d %H:%M:%S') if status.last_attempt else 'Never'
                next_try = status.next_attempt.strftime('%Y-%m-%d %H:%M:%S') if status.next_attempt else 'N/A'
                status_rows = f"""
                <dt>Attempts</dt><dd>{status.attempts} / {t.max_retry_days}</dd>
                <dt>Last Try</dt><dd>{last_try}</dd>
                <dt>Next Try</dt><dd>{next_try}</dd>
                """
                if status.last_result:
                    result_text = 'Success' if status.last_result.success else (status.last_result.error or 'Failed')
                    status_rows += f"<dt>Last Result</dt><dd>{result_text}</dd>"

            btn_text = 'Disable' if t.enabled else 'Enable'

            cards.append(f"""
            <div class="card" id="card-{t.id}">
                <div class="card-header">
                    <span class="card-title">{t.venue_name}</span>
                    {badge}
                </div>
                <dl class="info">
                    <dt>Platform</dt><dd>{t.platform}</dd>
                    <dt>Meal</dt><dd>{t.meal_type.value.title()}</dd>
                    <dt>Party</dt><dd>{t.party_size}</dd>
                    <dt>Window</dt><dd>{window_str}</dd>
                    <dt>Drop</dt><dd>{t.drop_time.strftime('%H:%M:%S')} {t.drop_timezone}</dd>
                    <dt>Advance</dt><dd>{t.days_in_advance} days</dd>
                    {status_rows}
                </dl>
                <div style="margin-top: 12px;">
                    <button class="btn" onclick="toggleTarget('{t.id}')">{btn_text}</button>
                </div>
            </div>
            """)
        cards_html = '<div class="grid">' + "\n".join(cards) + '</div>'

    return base.replace("{{CARDS}}", cards_html)


def create_app(config_dir=None) -> FastAPI:
    app = FastAPI(title="resbot Dashboard")

    scheduler = None
    event_queue = asyncio.Queue()

    @app.on_event("startup")
    async def startup():
        nonlocal scheduler
        try:
            profile = load_profile(config_dir)
            targets = load_targets(config_dir)
        except FileNotFoundError:
            logger.warning("No profile/targets found. Dashboard running in view-only mode.")
            return

        scheduler = ReservationScheduler(profile)

        def on_result(result):
            asyncio.get_event_loop().call_soon_threadsafe(
                event_queue.put_nowait,
                {"type": "result", "data": result.model_dump(mode="json")},
            )

        scheduler.on_result(on_result)
        for t in targets:
            if t.enabled:
                scheduler.add_target(t)
        await scheduler.start()

    @app.on_event("shutdown")
    async def shutdown():
        if scheduler:
            await scheduler.stop()

    @app.get("/", response_class=HTMLResponse)
    async def dashboard():
        targets = load_targets(config_dir)
        statuses = dict(scheduler.statuses) if scheduler else {}
        html = _render_dashboard(targets, statuses)
        return HTMLResponse(content=html)

    @app.get("/api/status")
    async def api_status():
        if not scheduler:
            return {"targets": [], "jobs": []}
        statuses = {
            tid: s.model_dump(mode="json")
            for tid, s in scheduler.statuses.items()
        }
        return {
            "targets": statuses,
            "jobs": scheduler.get_jobs_info(),
        }

    @app.post("/api/targets/{target_id}/toggle")
    async def toggle_target(target_id: str):
        if not scheduler:
            return {"error": "Scheduler not running"}
        status = scheduler.statuses.get(target_id)
        if not status:
            return {"error": "Target not found"}
        if status.enabled:
            scheduler.remove_target(target_id)
            status.enabled = False
        else:
            targets = load_targets(config_dir)
            for t in targets:
                if t.id == target_id:
                    scheduler.add_target(t)
                    status.enabled = True
                    break
        return {"target_id": target_id, "enabled": status.enabled}

    @app.get("/api/events")
    async def events(request: Request):
        async def event_generator():
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(event_queue.get(), timeout=30)
                    yield {"event": event["type"], "data": json.dumps(event["data"])}
                except asyncio.TimeoutError:
                    yield {"event": "ping", "data": ""}

        return EventSourceResponse(event_generator())

    return app
