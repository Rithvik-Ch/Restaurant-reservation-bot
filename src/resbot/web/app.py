"""FastAPI web dashboard for resbot status monitoring and management."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import date, datetime, timedelta
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import ValidationError
from sse_starlette.sse import EventSourceResponse

from resbot.config import (
    ensure_config_dir,
    load_profile,
    load_target,
    load_targets,
    remove_target,
    save_profile,
    save_target,
)
from resbot.activity_log import log_attempt, read_logs
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
        tomorrow = (datetime.now().date() + timedelta(days=1)).isoformat()
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
                <dt>Dates</dt><dd>{t.start_date or 'auto'} &rarr; {t.end_date or 'no limit'}</dd>
                <dt>Rate</dt><dd>{t.snipe_rate} req/sec</dd>
                <dt>Timeout</dt><dd>{t.snipe_timeout}s</dd>
                <dt>Watch</dt><dd>{f'{t.watch_duration}min (every {t.watch_interval}s)' if t.watch_duration else 'off'}</dd>
                {status_rows}
            </dl>
            <div class="mode-section" style="margin-top: 12px; padding-top: 12px; border-top: 1px solid #30363d;">
                <div style="display: flex; gap: 8px; align-items: center; margin-bottom: 8px;">
                    <label style="font-size: 0.8rem; color: #8b949e;">Date:</label>
                    <input type="date" id="action-date-{t.id}" value="{tomorrow}"
                           style="padding: 4px 8px; background: #0d1117; border: 1px solid #30363d; border-radius: 4px; color: #c9d1d9; font-size: 0.8rem;" />
                </div>
                <div style="display: flex; gap: 6px; flex-wrap: wrap;">
                    <button class="btn btn-primary" onclick="runAction('{t.id}', 'grab')" id="btn-grab-{t.id}" title="Immediately find and book an open slot">Grab Now</button>
                    <button class="btn" style="background:#1f6feb; border-color:#1f6feb; color:#fff;" onclick="runAction('{t.id}', 'snipe')" id="btn-snipe-{t.id}" title="Burst-loop at drop time to snag a slot">Snipe</button>
                    <button class="btn" onclick="toggleTarget('{t.id}')">{btn_text}</button>
                </div>
                <div id="action-status-{t.id}" style="margin-top: 8px; font-size: 0.8rem;"></div>
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
            log_attempt(
                target_id=result.target_id, action="run", target_date="scheduled",
                success=result.success, detail=result.error or "Reservation confirmed",
                confirmation=result.confirmation_token, config_dir=config_dir,
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

    # ── Action API (grab / snipe) ──

    _running_actions: dict[str, asyncio.Task] = {}

    @app.post("/api/targets/{target_id}/grab")
    async def grab_target(target_id: str, request: Request):
        """Immediately find and book an open slot."""
        data = await request.json()
        grab_date_str = data.get("date")
        if not grab_date_str:
            return JSONResponse(status_code=400, content={"error": "date is required"})
        try:
            grab_date = date.fromisoformat(grab_date_str)
        except ValueError:
            return JSONResponse(status_code=400, content={"error": "Invalid date format"})
        if grab_date < date.today():
            return JSONResponse(status_code=400, content={"error": "Date is in the past"})

        action_key = f"grab-{target_id}"
        if action_key in _running_actions and not _running_actions[action_key].done():
            return JSONResponse(status_code=409, content={"error": "Grab already running for this target"})

        async def _do_grab():
            try:
                from resbot.engine import rank_slots
                from resbot.platforms.resy import ResyClient

                profile = load_profile(config_dir)
                target = load_target(target_id, config_dir)
                client = ResyClient(profile)
                try:
                    await client.warmup()
                    slots, raw = await client.find_slots(target.venue_id, grab_date, target.party_size)

                    # --- Diagnostic: no slots from API ---
                    if not slots:
                        venues = raw.get("results", {}).get("venues", [])
                        if venues:
                            diag = (
                                f"No available time slots for {target.venue_name} on {grab_date}. "
                                f"The restaurant is listed on Resy but all slots appear fully booked "
                                f"for party size {target.party_size}."
                            )
                        elif raw.get("results") is not None:
                            diag = (
                                f"Resy returned no venue data for {target.venue_name} (ID: {target.venue_id}) "
                                f"on {grab_date}. The restaurant may not accept reservations on this date, "
                                f"or the venue ID may be incorrect."
                            )
                        else:
                            diag = (
                                f"Resy API returned an unexpected response for venue {target.venue_id} on {grab_date}. "
                                f"Response keys: {list(raw.keys()) if raw else 'empty'}. "
                                f"This may indicate an API issue or invalid venue ID."
                            )
                        result = BookingResult(target_id=target_id, success=False, error=diag)
                        event_queue.put_nowait({"type": "result", "data": result.model_dump(mode="json")})
                        log_attempt(target_id=target_id, action="grab", target_date=str(grab_date),
                                    success=False, detail=diag, venue_name=target.venue_name, config_dir=config_dir)
                        return

                    # --- Diagnostic: slots found, rank them ---
                    window = target.effective_window
                    ranked = rank_slots(slots, target)
                    window_str = f"{window.earliest.strftime('%H:%M')}-{window.latest.strftime('%H:%M')}"
                    slot_times = ", ".join(s.slot_time.strftime("%H:%M") for s in slots[:10])
                    diag_prefix = (
                        f"Found {len(slots)} slot(s): [{slot_times}{'...' if len(slots) > 10 else ''}]. "
                        f"Window {window_str} -> {len(ranked)} match(es). "
                    )

                    # --- Attempt to book top matches ---
                    book_errors = []
                    for slot in ranked[:3]:
                        try:
                            token = await client.get_booking_token(slot, grab_date, target.party_size)
                            result = await client.book(token)
                            if result.success:
                                result.target_id = target_id
                                event_queue.put_nowait({"type": "result", "data": result.model_dump(mode="json")})
                                log_attempt(target_id=target_id, action="grab", target_date=str(grab_date),
                                            success=True, detail=diag_prefix + f"Booked {slot.slot_time.strftime('%H:%M')}",
                                            venue_name=target.venue_name, confirmation=result.confirmation_token,
                                            config_dir=config_dir)
                                return
                        except Exception as e:
                            book_errors.append(f"{slot.slot_time.strftime('%H:%M')}: {e}")
                            continue

                    # All booking attempts failed
                    if book_errors:
                        err_detail = "; ".join(book_errors)
                        diag = diag_prefix + f"Booking failed for all attempted slots — {err_detail}"
                    else:
                        diag = diag_prefix + "No bookable slots after ranking."
                    result = BookingResult(target_id=target_id, success=False, error=diag)
                    event_queue.put_nowait({"type": "result", "data": result.model_dump(mode="json")})
                    log_attempt(target_id=target_id, action="grab", target_date=str(grab_date),
                                success=False, detail=diag, venue_name=target.venue_name, config_dir=config_dir)
                finally:
                    await client.close()
            except Exception as e:
                diag = f"System error: {type(e).__name__}: {e}"
                result = BookingResult(target_id=target_id, success=False, error=diag)
                event_queue.put_nowait({"type": "result", "data": result.model_dump(mode="json")})
                log_attempt(target_id=target_id, action="grab", target_date=str(grab_date),
                            success=False, detail=diag, config_dir=config_dir)

        task = asyncio.create_task(_do_grab())
        _running_actions[action_key] = task
        return {"status": "started", "action": "grab", "target_id": target_id, "date": grab_date_str}

    @app.post("/api/targets/{target_id}/snipe")
    async def snipe_target(target_id: str, request: Request):
        """Start a snipe burst loop for a target."""
        data = await request.json()
        snipe_date_str = data.get("date")

        action_key = f"snipe-{target_id}"
        if action_key in _running_actions and not _running_actions[action_key].done():
            return JSONResponse(status_code=409, content={"error": "Snipe already running for this target"})

        async def _do_snipe():
            try:
                from resbot.platforms.resy import ResyClient
                from resbot.runner import _compute_snipe_date

                profile = load_profile(config_dir)
                target = load_target(target_id, config_dir)
                override = date.fromisoformat(snipe_date_str) if snipe_date_str else None
                snipe_date = _compute_snipe_date(target, override)

                if snipe_date is None:
                    diag = f"Target date is past end_date ({target.end_date}). Update the target's date range."
                    result = BookingResult(target_id=target_id, success=False, error=diag)
                    event_queue.put_nowait({"type": "result", "data": result.model_dump(mode="json")})
                    log_attempt(target_id=target_id, action="snipe", target_date="N/A",
                                success=False, detail=diag, venue_name=target.venue_name, config_dir=config_dir)
                    return

                client = ResyClient(profile)
                try:
                    await client.warmup()
                    result = await client.snipe(target, snipe_date)
                    result.target_id = target_id
                    event_queue.put_nowait({"type": "result", "data": result.model_dump(mode="json")})
                    log_attempt(target_id=target_id, action="snipe", target_date=str(snipe_date),
                                success=result.success, detail=result.error or "Reservation confirmed",
                                venue_name=target.venue_name, confirmation=result.confirmation_token,
                                config_dir=config_dir)
                finally:
                    await client.close()
            except Exception as e:
                diag = f"System error: {type(e).__name__}: {e}"
                result = BookingResult(target_id=target_id, success=False, error=diag)
                event_queue.put_nowait({"type": "result", "data": result.model_dump(mode="json")})
                log_attempt(target_id=target_id, action="snipe", target_date=snipe_date_str or "auto",
                            success=False, detail=diag, config_dir=config_dir)

        task = asyncio.create_task(_do_snipe())
        _running_actions[action_key] = task
        return {"status": "started", "action": "snipe", "target_id": target_id}

    @app.post("/api/targets/{target_id}/stop")
    async def stop_action(target_id: str):
        """Cancel a running grab or snipe action."""
        stopped = []
        for prefix in ("grab", "snipe"):
            key = f"{prefix}-{target_id}"
            task = _running_actions.get(key)
            if task and not task.done():
                task.cancel()
                stopped.append(prefix)
        if stopped:
            return {"status": "stopped", "actions": stopped}
        return {"status": "nothing_running"}

    # ── Logs API ──

    @app.get("/api/logs")
    async def get_logs(days: int = 7):
        """Return recent activity log entries."""
        entries = read_logs(days=min(days, 30), config_dir=config_dir)
        return entries

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
