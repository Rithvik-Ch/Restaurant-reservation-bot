"""Scheduler for automated reservation sniping.

Manages drop-time scheduling, connection warmup, and daily retries
using APScheduler.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta, time as dt_time
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from resbot.config import load_profile, load_targets
from resbot.models import BookingResult, ReservationTarget, TargetStatus, UserProfile
from resbot.platforms.base import ReservationPlatform
from resbot.platforms.resy import ResyClient

logger = logging.getLogger(__name__)


class ReservationScheduler:
    """Manages scheduled snipe attempts for multiple targets."""

    def __init__(self, profile: UserProfile | None = None):
        self._profile = profile
        self._scheduler = AsyncIOScheduler()
        self._clients: dict[str, ReservationPlatform] = {}
        self._statuses: dict[str, TargetStatus] = {}
        self._callbacks: list = []

    @property
    def statuses(self) -> dict[str, TargetStatus]:
        return self._statuses

    def on_result(self, callback) -> None:
        """Register a callback for booking results."""
        self._callbacks.append(callback)

    def _get_client(self, target: ReservationTarget) -> ReservationPlatform:
        """Get or create a platform client for a target."""
        key = f"{target.platform}:{id(self._profile)}"
        if key not in self._clients:
            if target.platform == "resy":
                self._clients[key] = ResyClient(self._profile)
            elif target.platform == "opentable":
                from resbot.platforms.opentable import OpenTableClient
                self._clients[key] = OpenTableClient(self._profile)
            else:
                raise ValueError(f"Unsupported platform: {target.platform}")
        return self._clients[key]

    def _compute_target_date(self, target: ReservationTarget) -> date:
        """Compute the reservation date based on days_in_advance."""
        if target.target_date:
            return target.target_date
        tz = ZoneInfo(target.drop_timezone)
        now = datetime.now(tz)
        return (now + timedelta(days=target.days_in_advance)).date()

    def add_target(self, target: ReservationTarget) -> None:
        """Schedule a target for automated sniping."""
        if not target.enabled:
            logger.info("Skipping disabled target: %s", target.id)
            return

        self._statuses[target.id] = TargetStatus(
            target_id=target.id, enabled=True
        )

        tz = ZoneInfo(target.drop_timezone)
        drop = target.drop_time

        # Schedule warmup 30 seconds before drop
        warmup_time = _subtract_seconds(drop, 30)
        self._scheduler.add_job(
            self._warmup,
            CronTrigger(
                hour=warmup_time.hour,
                minute=warmup_time.minute,
                second=warmup_time.second,
                timezone=tz,
            ),
            args=[target],
            id=f"warmup:{target.id}",
            replace_existing=True,
            name=f"Warmup for {target.venue_name}",
        )

        # Schedule snipe at drop time
        self._scheduler.add_job(
            self._execute_snipe,
            CronTrigger(
                hour=drop.hour,
                minute=drop.minute,
                second=drop.second,
                timezone=tz,
            ),
            args=[target],
            id=f"snipe:{target.id}",
            replace_existing=True,
            name=f"Snipe {target.venue_name}",
        )

        # Calculate next attempt time
        now = datetime.now(tz)
        next_drop = now.replace(
            hour=drop.hour, minute=drop.minute, second=drop.second, microsecond=0
        )
        if next_drop <= now:
            next_drop += timedelta(days=1)
        self._statuses[target.id].next_attempt = next_drop

        logger.info(
            "Scheduled %s: snipe at %s %s, target date %s ahead",
            target.id,
            drop.isoformat(),
            target.drop_timezone,
            f"{target.days_in_advance} days",
        )

    def remove_target(self, target_id: str) -> None:
        """Remove a target from scheduling."""
        for prefix in ("warmup:", "snipe:"):
            job_id = f"{prefix}{target_id}"
            job = self._scheduler.get_job(job_id)
            if job:
                job.remove()
        self._statuses.pop(target_id, None)

    async def _warmup(self, target: ReservationTarget) -> None:
        """Warm up the connection before drop time."""
        logger.info("Warming up connection for %s", target.venue_name)
        client = self._get_client(target)
        await client.warmup()

    async def _execute_snipe(self, target: ReservationTarget) -> None:
        """Execute a snipe attempt for a target."""
        status = self._statuses.get(target.id)
        if not status or status.completed:
            return

        target_date = self._compute_target_date(target)
        client = self._get_client(target)

        logger.info(
            "Sniping %s for %s (party of %d) on %s",
            target.venue_name,
            target.meal_type.value,
            target.party_size,
            target_date.isoformat(),
        )

        status.attempts += 1
        status.last_attempt = datetime.now()

        try:
            result = await client.snipe(target, target_date)
        except Exception as e:
            result = BookingResult(
                target_id=target.id, success=False, error=str(e)
            )
            logger.error("Snipe failed for %s: %s", target.id, e)

        status.last_result = result

        if result.success:
            logger.info(
                "SUCCESS! Booked %s at %s for %s",
                target.venue_name,
                result.booked_time,
                target_date,
            )
            status.completed = True
            self.remove_target(target.id)
            self._statuses[target.id] = status  # Keep status after removing jobs
        else:
            if status.attempts >= target.max_retry_days:
                logger.warning(
                    "Max retries (%d) reached for %s. Giving up.",
                    target.max_retry_days,
                    target.id,
                )
                status.completed = True
                self.remove_target(target.id)
                self._statuses[target.id] = status
            else:
                tz = ZoneInfo(target.drop_timezone)
                now = datetime.now(tz)
                next_drop = now + timedelta(days=1)
                next_drop = next_drop.replace(
                    hour=target.drop_time.hour,
                    minute=target.drop_time.minute,
                    second=target.drop_time.second,
                    microsecond=0,
                )
                status.next_attempt = next_drop
                logger.info(
                    "Attempt %d/%d failed for %s. Next try: %s",
                    status.attempts,
                    target.max_retry_days,
                    target.id,
                    next_drop.isoformat(),
                )

        for cb in self._callbacks:
            try:
                cb(result)
            except Exception:
                pass

    async def start(self) -> None:
        """Start the scheduler."""
        self._scheduler.start()
        logger.info("Scheduler started with %d jobs", len(self._scheduler.get_jobs()))

    async def stop(self) -> None:
        """Stop the scheduler and close all clients."""
        self._scheduler.shutdown(wait=False)
        for client in self._clients.values():
            await client.close()
        self._clients.clear()

    def get_jobs_info(self) -> list[dict]:
        """Get info about all scheduled jobs."""
        jobs = []
        for job in self._scheduler.get_jobs():
            jobs.append(
                {
                    "id": job.id,
                    "name": job.name,
                    "next_run": str(job.next_run_time) if job.next_run_time else None,
                }
            )
        return jobs


def _subtract_seconds(t: dt_time, seconds: int) -> dt_time:
    """Subtract seconds from a time, wrapping around midnight."""
    total = t.hour * 3600 + t.minute * 60 + t.second - seconds
    if total < 0:
        total += 86400
    h, remainder = divmod(total, 3600)
    m, s = divmod(remainder, 60)
    return dt_time(h, m, s)
