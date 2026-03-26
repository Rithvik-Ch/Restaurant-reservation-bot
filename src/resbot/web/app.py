"""FastAPI web dashboard for resbot status monitoring."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sse_starlette.sse import EventSourceResponse

from resbot.config import load_profile, load_targets
from resbot.models import BookingResult, ReservationTarget, TargetStatus
from resbot.scheduler import ReservationScheduler

logger = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).parent / "templates"


def create_app(config_dir: Path | None = None) -> FastAPI:
    app = FastAPI(title="resbot Dashboard")
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

    scheduler: ReservationScheduler | None = None
    event_queue: asyncio.Queue = asyncio.Queue()

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

        def on_result(result: BookingResult):
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
    async def dashboard(request: Request):
        targets = load_targets(config_dir)
        statuses = scheduler.statuses if scheduler else {}
        return templates.TemplateResponse(
            "dashboard.html",
            {
                "request": request,
                "targets": targets,
                "statuses": statuses,
                "now": datetime.now().isoformat(),
            },
        )

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
