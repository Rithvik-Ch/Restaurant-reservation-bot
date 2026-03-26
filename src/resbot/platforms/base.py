"""Abstract base class for reservation platform adapters."""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date

from resbot.models import BookingResult, ReservationTarget, Slot, UserProfile


class ReservationPlatform(ABC):
    """Base interface for all reservation platform clients."""

    @abstractmethod
    async def authenticate(self, profile: UserProfile) -> None:
        """Validate credentials and establish session."""

    @abstractmethod
    async def search_venues(self, query: str) -> list[dict]:
        """Search for venues by name/location. Returns list of venue dicts."""

    @abstractmethod
    async def find_slots(
        self, venue_id: str, day: date, party_size: int
    ) -> list[Slot]:
        """Find available reservation slots for a venue on a date."""

    @abstractmethod
    async def get_booking_token(
        self, slot: Slot, day: date, party_size: int
    ) -> str:
        """Get a booking token for a specific slot."""

    @abstractmethod
    async def book(self, booking_token: str) -> BookingResult:
        """Execute the booking with the given token."""

    @abstractmethod
    async def warmup(self) -> None:
        """Warm up the connection (pre-snipe)."""

    @abstractmethod
    async def close(self) -> None:
        """Close the client session."""

    async def snipe(self, target: ReservationTarget, day: date) -> BookingResult:
        """Full snipe pipeline: find → rank → book. Override for platform-specific optimizations."""
        from resbot.engine import rank_slots

        slots = await self.find_slots(target.venue_id, day, target.party_size)
        ranked = rank_slots(slots, target)
        if not ranked:
            return BookingResult(
                target_id=target.id, success=False, error="No matching slots found"
            )
        for slot in ranked:
            try:
                token = await self.get_booking_token(slot, day, target.party_size)
                result = await self.book(token)
                if result.success:
                    result.target_id = target.id
                    return result
            except Exception as e:
                continue
        return BookingResult(
            target_id=target.id, success=False, error="All slot booking attempts failed"
        )
