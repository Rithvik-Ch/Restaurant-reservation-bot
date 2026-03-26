"""Tests for the reservation engine slot ranking."""

from datetime import date, time

from resbot.engine import rank_slots
from resbot.models import MealTime, ReservationTarget, Slot, TimeWindow


def _make_slot(hour: int, minute: int = 0, table_type: str = "") -> Slot:
    return Slot(
        config_token=f"token-{hour}:{minute:02d}",
        slot_time=time(hour, minute),
        date=date(2025, 6, 15),
        table_type=table_type,
    )


def _make_target(**kwargs) -> ReservationTarget:
    defaults = {
        "id": "test",
        "venue_id": "123",
        "venue_name": "Test",
        "party_size": 2,
        "meal_type": MealTime.DINNER,
    }
    defaults.update(kwargs)
    return ReservationTarget(**defaults)


def test_rank_slots_filters_by_window():
    slots = [_make_slot(17, 0), _make_slot(19, 0), _make_slot(22, 0)]
    target = _make_target()  # dinner default: 17:30-21:00
    ranked = rank_slots(slots, target)
    assert len(ranked) == 1
    assert ranked[0].slot_time == time(19, 0)


def test_rank_slots_sorts_by_preferred_time():
    slots = [_make_slot(18, 0), _make_slot(19, 0), _make_slot(20, 0)]
    target = _make_target(
        time_window=TimeWindow(earliest=time(17, 0), latest=time(21, 0))
    )
    # Default dinner preferred = 19:00
    ranked = rank_slots(slots, target)
    assert ranked[0].slot_time == time(19, 0)
    assert ranked[1].slot_time in (time(18, 0), time(20, 0))


def test_rank_slots_custom_preferred_times():
    slots = [_make_slot(18, 0), _make_slot(18, 30), _make_slot(19, 0), _make_slot(20, 0)]
    target = _make_target(
        time_window=TimeWindow(earliest=time(17, 0), latest=time(21, 0)),
        preferred_times=[time(18, 30)],
    )
    ranked = rank_slots(slots, target)
    assert ranked[0].slot_time == time(18, 30)


def test_rank_slots_seating_preference():
    slots = [
        _make_slot(19, 0, table_type="Bar"),
        _make_slot(19, 0, table_type="Dining Room"),
        _make_slot(19, 30, table_type="Patio"),
    ]
    target = _make_target(
        time_window=TimeWindow(earliest=time(17, 0), latest=time(21, 0)),
        preferred_seating="Dining Room",
    )
    ranked = rank_slots(slots, target)
    assert all("dining room" in s.table_type.lower() for s in ranked)


def test_rank_slots_seating_fallback():
    """If no slots match seating pref, return all matching time window."""
    slots = [_make_slot(19, 0, table_type="Bar")]
    target = _make_target(
        time_window=TimeWindow(earliest=time(17, 0), latest=time(21, 0)),
        preferred_seating="Dining Room",
    )
    ranked = rank_slots(slots, target)
    assert len(ranked) == 1  # Falls back to bar since no dining room


def test_rank_slots_empty():
    ranked = rank_slots([], _make_target())
    assert ranked == []
