"""Reservation engine: slot ranking and orchestration."""

from __future__ import annotations

from datetime import time

from resbot.models import ReservationTarget, Slot


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
    """
    window = target.effective_window
    preferred = target.effective_preferred_times

    # Filter by time window
    filtered = [
        s for s in slots
        if _time_in_window(s.slot_time, window.earliest, window.latest)
    ]

    # Filter by seating preference
    if target.preferred_seating:
        seating_match = [
            s for s in filtered
            if target.preferred_seating.lower() in s.table_type.lower()
        ]
        # Fall back to all if no seating match
        if seating_match:
            filtered = seating_match

    # Sort by minimum distance to any preferred time
    def sort_key(slot: Slot) -> int:
        return min(_time_distance(slot.slot_time, p) for p in preferred)

    filtered.sort(key=sort_key)
    return filtered
