"""Core data models for resbot."""

from __future__ import annotations

from datetime import date, datetime, time
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field


class MealTime(str, Enum):
    """Meal types with their ideal target times."""

    BREAKFAST = "breakfast"  # 09:00
    BRUNCH = "brunch"  # 12:00
    LUNCH = "lunch"  # 13:00
    DINNER = "dinner"  # 19:00

    @property
    def ideal_time(self) -> time:
        return {
            MealTime.BREAKFAST: time(9, 0),
            MealTime.BRUNCH: time(12, 0),
            MealTime.LUNCH: time(13, 0),
            MealTime.DINNER: time(19, 0),
        }[self]

    @property
    def default_window(self) -> TimeWindow:
        windows = {
            MealTime.BREAKFAST: TimeWindow(earliest=time(8, 0), latest=time(10, 30)),
            MealTime.BRUNCH: TimeWindow(earliest=time(10, 30), latest=time(13, 30)),
            MealTime.LUNCH: TimeWindow(earliest=time(11, 30), latest=time(14, 30)),
            MealTime.DINNER: TimeWindow(earliest=time(17, 30), latest=time(21, 0)),
        }
        return windows[self]


class TimeWindow(BaseModel):
    """Acceptable time range for a reservation."""

    earliest: time
    latest: time


class UserProfile(BaseModel):
    """User profile with platform credentials."""

    name: str
    phone: str
    email: str
    resy_api_key: str = ""
    resy_auth_token: str = ""
    resy_email: str = ""
    resy_password: str = ""
    resy_payment_method_id: str | None = None
    opentable_email: str = ""
    opentable_password: str = ""


class ReservationTarget(BaseModel):
    """A single reservation automation target."""

    id: str
    platform: Literal["resy", "opentable"] = "resy"
    venue_id: str
    venue_name: str
    party_size: int = Field(ge=1, le=20)
    meal_type: MealTime
    time_window: TimeWindow | None = None
    preferred_times: list[time] = Field(default_factory=list)
    preferred_seating: str | None = None
    target_date: date | None = None
    start_date: date | None = None
    end_date: date | None = None
    days_in_advance: int = Field(default=14, ge=1)
    drop_time: time = Field(default_factory=lambda: time(0, 0, 0))
    drop_timezone: str = "America/New_York"
    max_retry_days: int = Field(default=30, ge=1)
    snipe_rate: float = Field(default=10.0, ge=1.0, le=50.0)
    snipe_timeout: int = Field(default=300, ge=10)
    enabled: bool = True

    @property
    def effective_window(self) -> TimeWindow:
        return self.time_window or self.meal_type.default_window

    @property
    def effective_preferred_times(self) -> list[time]:
        return self.preferred_times or [self.meal_type.ideal_time]


class Slot(BaseModel):
    """A single available reservation slot."""

    config_token: str
    slot_time: time
    date: date
    table_type: str = ""
    shift_label: str = ""
    payment_required: bool = False


class BookingResult(BaseModel):
    """Result of a booking attempt."""

    target_id: str
    success: bool
    reservation_id: str | None = None
    confirmation_token: str | None = None
    booked_time: datetime | None = None
    error: str | None = None


class TargetStatus(BaseModel):
    """Runtime status of a reservation target."""

    target_id: str
    enabled: bool = True
    attempts: int = 0
    last_attempt: datetime | None = None
    last_result: BookingResult | None = None
    next_attempt: datetime | None = None
    completed: bool = False
