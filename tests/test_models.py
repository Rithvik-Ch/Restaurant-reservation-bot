"""Tests for core data models."""

from datetime import date, time

from resbot.models import (
    BookingResult,
    MealTime,
    ReservationTarget,
    Slot,
    TimeWindow,
    UserProfile,
)


def test_meal_time_ideal_times():
    assert MealTime.BREAKFAST.ideal_time == time(9, 0)
    assert MealTime.BRUNCH.ideal_time == time(12, 0)
    assert MealTime.LUNCH.ideal_time == time(13, 0)
    assert MealTime.DINNER.ideal_time == time(19, 0)


def test_meal_time_default_windows():
    w = MealTime.DINNER.default_window
    assert w.earliest == time(17, 30)
    assert w.latest == time(21, 0)


def test_reservation_target_effective_window():
    t = ReservationTarget(
        id="test",
        venue_id="123",
        venue_name="Test",
        party_size=2,
        meal_type=MealTime.DINNER,
    )
    # Without custom window, uses meal default
    assert t.effective_window.earliest == time(17, 30)

    # With custom window
    t2 = ReservationTarget(
        id="test2",
        venue_id="123",
        venue_name="Test",
        party_size=2,
        meal_type=MealTime.DINNER,
        time_window=TimeWindow(earliest=time(18, 0), latest=time(20, 0)),
    )
    assert t2.effective_window.earliest == time(18, 0)


def test_reservation_target_effective_preferred_times():
    t = ReservationTarget(
        id="test",
        venue_id="123",
        venue_name="Test",
        party_size=2,
        meal_type=MealTime.DINNER,
    )
    assert t.effective_preferred_times == [time(19, 0)]

    t2 = ReservationTarget(
        id="test2",
        venue_id="123",
        venue_name="Test",
        party_size=2,
        meal_type=MealTime.DINNER,
        preferred_times=[time(18, 30), time(19, 30)],
    )
    assert t2.effective_preferred_times == [time(18, 30), time(19, 30)]


def test_user_profile():
    p = UserProfile(
        name="Test User",
        phone="+15551234567",
        email="test@example.com",
        resy_api_key="key123",
        resy_auth_token="token123",
    )
    assert p.name == "Test User"
    assert p.resy_payment_method_id is None


def test_slot():
    s = Slot(
        config_token="abc123",
        slot_time=time(19, 0),
        date=date(2025, 6, 15),
        table_type="Dining Room",
    )
    assert s.config_token == "abc123"
    assert s.slot_time == time(19, 0)


def test_booking_result():
    r = BookingResult(target_id="test", success=True, confirmation_token="CONF123")
    assert r.success
    assert r.confirmation_token == "CONF123"
