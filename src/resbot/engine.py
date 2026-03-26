"""Reservation engine: slot ranking and orchestration."""

from __future__ import annotations

import logging
from datetime import time

from resbot.models import ReservationTarget, Slot

logger = logging.getLogger(__name__)


def _time_distance(a: time, b: time) -> int:
    """Distance between two times in minutes."""
    a_mins = a.hour * 60 + a.minute
    b_mins = b.hour * 60 + b.minute
    return abs(a_mins - b_mins)


def _time_in_window(t: time, earliest: time, latest: time) -> bool:
    """Check if a time falls within a window (inclusive)."""
    return earliest <= t <= latest


def rank_slots(slots: list[Slot], target: ReservationTarget) -> list[Slot]:
    """Filter and rank slots by preference.

    1. Filter to slots within the target's time window
    2. Filter by preferred seating type if set
    3. Sort by proximity to preferred times (closest first)
    4. If filtering removes everything, fall back to all slots sorted by preference
    """
    if not slots:
        logger.debug("No raw slots returned from API")
        return []

    window = target.effective_window
    preferred = target.effective_preferred_times

    logger.info(
        "Found %d raw slot(s). Filtering to window %s-%s",
        len(slots),
        window.earliest.strftime("%H:%M"),
        window.latest.strftime("%H:%M"),
    )
    for s in slots:
        logger.debug("  Raw slot: %s %s", s.slot_time.strftime("%H:%M"), s.table_type)

    # Filter by time window
    filtered = [
        s for s in slots
        if _time_in_window(s.slot_time, window.earliest, window.latest)
    ]

    if not filtered:
        logger.warning(
            "Time window %s-%s filtered out ALL %d slot(s). "
            "Falling back to all slots. Slot times were: %s",
            window.earliest.strftime("%H:%M"),
            window.latest.strftime("%H:%M"),
            len(slots),
            ", ".join(s.slot_time.strftime("%H:%M") for s in slots),
        )
        filtered = list(slots)
    else:
        logger.info("%d slot(s) within time window", len(filtered))

    # Filter by seating preference
    if target.preferred_seating:
        seating_match = [
            s for s in filtered
            if target.preferred_seating.lower() in s.table_type.lower()
        ]
        # Fall back to all if no seating match
        if seating_match:
            filtered = seating_match
            logger.info("%d slot(s) match seating '%s'", len(filtered), target.preferred_seating)
        else:
            logger.info(
                "No slots match seating '%s', using all %d slot(s)",
                target.preferred_seating,
                len(filtered),
            )

    # Sort by minimum distance to any preferred time
    def sort_key(slot: Slot) -> int:
        return min(_time_distance(slot.slot_time, p) for p in preferred)

    filtered.sort(key=sort_key)
    logger.info(
        "Returning %d ranked slot(s). Best: %s",
        len(filtered),
        filtered[0].slot_time.strftime("%H:%M") if filtered else "none",
    )
    return filtered
