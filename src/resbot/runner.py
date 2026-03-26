"""Async runner for concurrent reservation automations."""

from __future__ import annotations

import asyncio
import logging
import signal
from datetime import date as date_type

from resbot.config import load_profile, load_targets
from resbot.models import BookingResult, UserProfile
from resbot.scheduler import ReservationScheduler

logger = logging.getLogger(__name__)


async def run_scheduler(config_dir=None) -> None:
    """Load config and run the scheduler until interrupted."""
    profile = load_profile(config_dir)
    targets = load_targets(config_dir)

    if not targets:
        logger.error("No targets configured. Use 'resbot target add' first.")
        return

    enabled = [t for t in targets if t.enabled]
    if not enabled:
        logger.error("All targets are disabled.")
        return

    scheduler = ReservationScheduler(profile)

    def on_result(result: BookingResult) -> None:
        if result.success:
            logger.info("BOOKED: %s", result.target_id)
        else:
            logger.info("FAILED: %s - %s", result.target_id, result.error)

    scheduler.on_result(on_result)

    for target in enabled:
        scheduler.add_target(target)

    logger.info("Starting scheduler with %d target(s)...", len(enabled))
    for t in enabled:
        logger.info(
            "  - %s: %s (%s, party of %d)",
            t.id,
            t.venue_name,
            t.meal_type.value,
            t.party_size,
        )

    await scheduler.start()

    stop_event = asyncio.Event()
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_event.set)

    await stop_event.wait()
    logger.info("Shutting down scheduler...")
    await scheduler.stop()


def _compute_snipe_date(target, override_date=None):
    """Compute the date to snipe for, with optional override."""
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    if override_date:
        logger.info("Using override date: %s", override_date.isoformat())
        return override_date

    if target.target_date:
        target_date = target.target_date
    else:
        tz = ZoneInfo(target.drop_timezone)
        target_date = (datetime.now(tz) + timedelta(days=target.days_in_advance)).date()

    # Clamp to start_date / end_date
    if target.start_date and target_date < target.start_date:
        target_date = target.start_date
    if target.end_date and target_date > target.end_date:
        return None  # signals past end_date

    return target_date


async def run_single_snipe(
    target_id: str, config_dir=None, override_date: date_type | None = None
) -> BookingResult:
    """Run a single immediate snipe attempt for a target."""
    from resbot.config import load_profile, load_target
    from resbot.platforms.resy import ResyClient

    profile = load_profile(config_dir)
    target = load_target(target_id, config_dir)

    if target.platform == "resy":
        client = ResyClient(profile)
    else:
        raise ValueError(f"Unsupported platform: {target.platform}")

    try:
        await client.warmup()

        target_date = _compute_snipe_date(target, override_date)
        if target_date is None:
            return BookingResult(
                target_id=target.id,
                success=False,
                error=f"Target date is past end_date {target.end_date}",
            )

        logger.info("Sniping %s for date %s", target.venue_name, target_date.isoformat())
        result = await client.snipe(target, target_date)
        return result
    finally:
        await client.close()


async def run_all_snipes(
    config_dir=None, override_date: date_type | None = None
) -> list[BookingResult]:
    """Run immediate snipe attempts for all enabled targets concurrently."""
    from resbot.config import load_profile, load_targets
    from resbot.platforms.resy import ResyClient

    profile = load_profile(config_dir)
    targets = [t for t in load_targets(config_dir) if t.enabled]

    if not targets:
        return []

    async def snipe_one(target):
        if target.platform == "resy":
            client = ResyClient(profile)
        else:
            raise ValueError(f"Unsupported platform: {target.platform}")
        try:
            await client.warmup()

            target_date = _compute_snipe_date(target, override_date)
            if target_date is None:
                return BookingResult(
                    target_id=target.id,
                    success=False,
                    error=f"Target date is past end_date {target.end_date}",
                )

            return await client.snipe(target, target_date)
        finally:
            await client.close()

    results = await asyncio.gather(
        *(snipe_one(t) for t in targets), return_exceptions=True
    )

    final = []
    for i, r in enumerate(results):
        if isinstance(r, Exception):
            final.append(
                BookingResult(
                    target_id=targets[i].id, success=False, error=str(r)
                )
            )
        else:
            final.append(r)
    return final
