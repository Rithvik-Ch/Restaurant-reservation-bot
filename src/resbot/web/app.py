"""FastAPI web dashboard for resbot status monitoring and management."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import ValidationError
from sse_starlette.sse import EventSourceResponse

from resbot.config import (
    ensure_config_dir,
    load_profile,
    load_targets,
    remove_target,
    save_profile,
    save_target,
)
from resbot.models import BookingResult, ReservationTarget, UserProfile
from resbot.scheduler import ReservationScheduler

logger = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).parent / "templates"


def _redact(s: str, show: int = 6) -> str:
    if not s:
        return ""
    if len(s) <= show:
        return "***"
    return s[:show] + "***"


def _render_dashboard(targets, statuses) -> str:
    """Render dashboard HTML with target status cards."""
    if not targets:
        return '<div class="empty"><p>No targets configured. Go to the Targets tab to add one.</p></div>'
    cards = []
    for t in targets:
        status = statuses.get(t.id)
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
        status_rows = ""
        if status:
            last_try = status.last_attempt.strftime('%Y-%m-%d %H:%M:%S') if status.last_attempt else 'Never'
            next_try = status.next_attempt.strftime('%Y-%m-%d %H:%M:%S') if status.next_attempt else 'N/A'
            status_rows = f"""
            <dt>Attempts</dt><dd>{status.attempts} / {t.max_retry_days}</dd>
            <dt>Last Try</dt><dd>{last_try}</dd>
            <dt>Next Try</dt><dd>{next_try}</dd>"""
            if status.last_result:
                result_text = 'Success' if status.last_result.success else (status.last_result.error or 'Failed')
                status_rows += f"\n            <dt>Last Result</dt><dd>{result_text}</dd>"
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
                <dt>Rate</dt><dd>{t.snipe_rate} req/sec</dd>
                <dt>Timeout</dt><dd>{t.snipe_timeout}s</dd>
                {status_rows}
            </dl>
            <div style="margin-top: 12px;">
                <button class="btn" onclick="toggleTarget('{t.id}')">{btn_text}</button>
            </div>
        </div>""")
    return '<div class="grid">' + "\n".join(cards) + '</div>'


def create_app(config_dir=None) -> FastAPI:
    app = FastAPI(title="resbot Dashboard")
    ensure_config_dir(config_dir)

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

    # ── Pages ──

    @app.get("/", response_class=HTMLResponse)
    async def dashboard():
        with open(TEMPLATES_DIR / "dashboard_base.html") as f:
            base = f.read()
        targets = load_targets(config_dir)
        statuses = dict(scheduler.statuses) if scheduler else {}
        cards_html = _render_dashboard(targets, statuses)
        return HTMLResponse(content=base.replace("{{CARDS}}", cards_html))

    # ── Profile API ──

    @app.get("/api/profile")
    async def get_profile():
        try:
            p = load_profile(config_dir)
            return {
                "name": p.name,
                "phone": p.phone,
                "email": p.email,
                "resy_api_key": _redact(p.resy_api_key),
                "resy_auth_token": _redact(p.resy_auth_token),
                "resy_email": p.resy_email,
                "resy_password": "********" if p.resy_password else "",
                "resy_payment_method_id": p.resy_payment_method_id or "",
                "opentable_email": p.opentable_email,
                "opentable_password": "********" if p.opentable_password else "",
            }
        except FileNotFoundError:
            return {"error": "No profile found"}

    @app.post("/api/profile")
    async def update_profile(request: Request):
        data = await request.json()
        try:
            existing = load_profile(config_dir)
        except FileNotFoundError:
            existing = None

        # Don't overwrite secrets with redacted placeholders
        if existing:
            if data.get("resy_api_key", "").endswith("***"):
                data["resy_api_key"] = existing.resy_api_key
            if data.get("resy_auth_token", "").endswith("***"):
                data["resy_auth_token"] = existing.resy_auth_token
            if data.get("resy_password") == "********":
                data["resy_password"] = existing.resy_password
            if data.get("opentable_password") == "********":
                data["opentable_password"] = existing.opentable_password

        try:
            p = UserProfile(**data)
            save_profile(p, config_dir)
            return {"status": "saved"}
        except ValidationError as e:
            return JSONResponse(status_code=400, content={"error": str(e)})

    @app.post("/api/profile/login")
    async def profile_login(request: Request):
        data = await request.json()
        email = data.get("email", "")
        password = data.get("password", "")
        if not email or not password:
            return JSONResponse(status_code=400, content={"error": "Email and password required"})
        try:
            from resbot.platforms.resy import ResyClient
            creds = await ResyClient.login(email, password)
            return {"status": "success", **creds}
        except Exception as e:
            return JSONResponse(status_code=400, content={"error": str(e)})

    # ── Targets API ──

    @app.get("/api/targets")
    async def list_targets():
        targets = load_targets(config_dir)
        return [t.model_dump(mode="json") for t in targets]

    @app.post("/api/targets")
    async def create_target(request: Request):
        data = await request.json()
        try:
            t = ReservationTarget(**data)
            save_target(t, config_dir)
            if scheduler and t.enabled:
                scheduler.add_target(t)
            return {"status": "created", "id": t.id}
        except ValidationError as e:
            return JSONResponse(status_code=400, content={"error": str(e)})

    @app.put("/api/targets/{target_id}")
    async def update_target(target_id: str, request: Request):
        data = await request.json()
        data["id"] = target_id
        try:
            t = ReservationTarget(**data)
            save_target(t, config_dir)
            if scheduler:
                scheduler.remove_target(target_id)
                if t.enabled:
                    scheduler.add_target(t)
            return {"status": "updated", "id": t.id}
        except ValidationError as e:
            return JSONResponse(status_code=400, content={"error": str(e)})

    @app.delete("/api/targets/{target_id}")
    async def delete_target(target_id: str):
        removed = remove_target(target_id, config_dir)
        if scheduler:
            scheduler.remove_target(target_id)
        if removed:
            return {"status": "deleted"}
        return JSONResponse(status_code=404, content={"error": "Target not found"})

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

    # ── Status API ──

    @app.get("/api/status")
    async def api_status():
        if not scheduler:
            return {"targets": [], "jobs": []}
        statuses = {
            tid: s.model_dump(mode="json")
            for tid, s in scheduler.statuses.items()
        }
        return {"targets": statuses, "jobs": scheduler.get_jobs_info()}

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
