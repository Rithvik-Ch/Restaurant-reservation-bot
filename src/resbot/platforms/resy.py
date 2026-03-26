"""Resy API client optimized for speed.

API flow:
1. GET /2/user         — validate auth, get payment method
2. GET /4/find         — find available slots
3. POST /3/details     — get booking token from slot
4. POST /3/book        — execute the booking
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, time

import httpx
import orjson

from resbot.models import BookingResult, ReservationTarget, Slot, UserProfile
from resbot.platforms.base import ReservationPlatform

logger = logging.getLogger(__name__)

BASE_URL = "https://api.resy.com"


class ResyClient(ReservationPlatform):
    """High-performance Resy API client with connection pooling and HTTP/2."""

    def __init__(self, profile: UserProfile):
        self._profile = profile
        self._payment_method_id = profile.resy_payment_method_id
        self._session = httpx.AsyncClient(
            base_url=BASE_URL,
            http2=True,
            timeout=httpx.Timeout(10.0, connect=3.0),
            headers={
                "Authorization": f'ResyAPI api_key="{profile.resy_api_key}"',
                "X-Resy-Auth-Token": profile.resy_auth_token,
                "Accept": "application/json",
                "Cache-Control": "no-cache",
            },
            limits=httpx.Limits(
                max_connections=20,
                max_keepalive_connections=10,
                keepalive_expiry=30,
            ),
        )

    @staticmethod
    async def login(email: str, password: str, api_key: str = "") -> dict:
        """Log in to Resy with email/password and return credentials.

        Returns dict with: auth_token, api_key, payment_method_id,
        first_name, last_name, phone.
        """
        # Resy's public API key (used by the website itself)
        if not api_key:
            api_key = "VbWk7s3L4KiK5fzlO7JD3Q5EYolJI7n5"
        async with httpx.AsyncClient(
            base_url=BASE_URL,
            http2=True,
            timeout=httpx.Timeout(10.0),
            headers={
                "Authorization": f'ResyAPI api_key="{api_key}"',
                "Accept": "application/json",
            },
        ) as client:
            resp = await client.post(
                "/3/auth/password",
                data={"email": email, "password": password},
            )
            resp.raise_for_status()
            data = orjson.loads(resp.content)

            auth_token = data.get("token", "")
            payment_method_id = ""
            payment_methods = data.get("payment_methods", [])
            if payment_methods:
                payment_method_id = str(payment_methods[0].get("id", ""))

            return {
                "auth_token": auth_token,
                "api_key": api_key,
                "payment_method_id": payment_method_id,
                "first_name": data.get("first_name", ""),
                "last_name": data.get("last_name", ""),
                "phone": data.get("mobile_number", ""),
            }

    async def authenticate(self, profile: UserProfile) -> None:
        """Validate auth token and fetch payment method ID if needed."""
        resp = await self._session.get("/2/user")
        resp.raise_for_status()
        data = orjson.loads(resp.content)
        if not self._payment_method_id:
            payment_methods = data.get("payment_methods", [])
            if payment_methods:
                self._payment_method_id = str(payment_methods[0].get("id", ""))
                logger.info("Auto-detected payment method: %s", self._payment_method_id)

    async def search_venues(self, query: str) -> list[dict]:
        """Search Resy for venues matching query."""
        # Try POST to the search endpoint (Resy has changed methods over time)
        for method, endpoint, payload in [
            ("POST", "/3/venuesearch/search", {"query": query, "per_page": 10, "types": ["venue"]}),
            ("GET", "/3/venuesearch/search", None),
        ]:
            try:
                if method == "POST":
                    resp = await self._session.post(endpoint, json=payload)
                else:
                    resp = await self._session.get(
                        endpoint, params={"query": query, "per_page": 10}
                    )
                resp.raise_for_status()
                data = orjson.loads(resp.content)
                results = []
                for hit in data.get("search", {}).get("hits", []):
                    results.append(
                        {
                            "venue_id": str(hit.get("id", {}).get("resy", "")),
                            "name": hit.get("name", ""),
                            "location": hit.get("location", {}).get("name", ""),
                            "cuisine": hit.get("cuisine", []),
                            "price_range": hit.get("price_range_id", 0),
                            "url_slug": hit.get("url_slug", ""),
                        }
                    )
                return results
            except Exception:
                continue
        raise RuntimeError("Venue search API is unavailable")

    async def find_slots(
        self, venue_id: str, day: date, party_size: int
    ) -> list[Slot]:
        """Find available slots. Optimized for minimal parsing."""
        resp = await self._session.get(
            "/4/find",
            params={
                "venue_id": venue_id,
                "day": day.isoformat(),
                "party_size": party_size,
                "lat": 0,
                "long": 0,
            },
        )
        resp.raise_for_status()
        data = orjson.loads(resp.content)

        slots = []
        for venue in data.get("results", {}).get("venues", []):
            for slot_data in venue.get("slots", []):
                config = slot_data.get("config", {})
                dt = slot_data.get("date", {})
                start_str = dt.get("start", "")
                if not start_str:
                    continue
                try:
                    slot_dt = datetime.fromisoformat(start_str)
                    slot_time = slot_dt.time()
                except (ValueError, TypeError):
                    continue
                slots.append(
                    Slot(
                        config_token=config.get("token", ""),
                        slot_time=slot_time,
                        date=day,
                        table_type=config.get("type", ""),
                        shift_label=slot_data.get("shift", {}).get("label", ""),
                        payment_required=bool(slot_data.get("payment", {}).get("is_paid")),
                    )
                )
        return slots

    async def get_booking_token(
        self, slot: Slot, day: date, party_size: int
    ) -> str:
        """Get booking token from a slot config token."""
        resp = await self._session.post(
            "/3/details",
            data={
                "config_id": slot.config_token,
                "day": day.isoformat(),
                "party_size": party_size,
            },
        )
        resp.raise_for_status()
        data = orjson.loads(resp.content)
        book_token = data.get("book_token", {}).get("value", "")
        if not book_token:
            raise ValueError("No booking token in response")
        return book_token

    async def book(self, booking_token: str) -> BookingResult:
        """Execute booking with pre-obtained token."""
        payment_body = orjson.dumps({"id": self._payment_method_id}).decode()
        resp = await self._session.post(
            "/3/book",
            data={
                "book_token": booking_token,
                "struct_payment_method": payment_body,
                "source_id": "resy.com-venue-details",
            },
        )
        resp.raise_for_status()
        data = orjson.loads(resp.content)

        resy_token = data.get("resy_token", "")
        reservation_id = data.get("reservation_id", "")

        return BookingResult(
            target_id="",
            success=True,
            reservation_id=str(reservation_id),
            confirmation_token=resy_token,
            booked_time=datetime.now(),
        )

    async def warmup(self) -> None:
        """Warm the HTTP/2 connection before snipe time."""
        try:
            await self._session.get("/2/user")
            logger.debug("Connection warmup complete")
        except Exception as e:
            logger.warning("Warmup failed: %s", e)

    async def close(self) -> None:
        """Close the HTTP client session."""
        await self._session.aclose()

    async def snipe(self, target: ReservationTarget, day: date) -> BookingResult:
        """Speed-optimized snipe: burst requests with configurable rate and timeout.

        Uses target.snipe_rate (requests/sec) and target.snipe_timeout (seconds)
        to control the burst. Stops immediately on success or when timeout expires.
        """
        import time as _time

        from resbot.engine import rank_slots

        best_result = BookingResult(
            target_id=target.id, success=False, error="No slots found"
        )

        sleep_interval = 1.0 / target.snipe_rate
        deadline = _time.monotonic() + target.snipe_timeout
        attempt = 0

        while _time.monotonic() < deadline:
            attempt += 1
            try:
                slots = await self.find_slots(target.venue_id, day, target.party_size)
                ranked = rank_slots(slots, target)
                if not ranked:
                    await asyncio.sleep(sleep_interval)
                    continue

                # Try top 3 slots in parallel
                top_slots = ranked[:3]
                tasks = [
                    self._try_book_slot(slot, day, target.party_size)
                    for slot in top_slots
                ]
                results = await asyncio.gather(*tasks, return_exceptions=True)

                for result in results:
                    if isinstance(result, BookingResult) and result.success:
                        result.target_id = target.id
                        logger.info("Snipe succeeded on attempt %d", attempt)
                        return result

                best_result = BookingResult(
                    target_id=target.id,
                    success=False,
                    error="Slots found but booking failed",
                )
                await asyncio.sleep(sleep_interval)
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 412:
                    await asyncio.sleep(sleep_interval)
                    continue
                logger.warning("HTTP error during snipe attempt %d: %s", attempt, e)
                await asyncio.sleep(sleep_interval)
            except Exception as e:
                logger.warning("Error during snipe attempt %d: %s", attempt, e)
                await asyncio.sleep(sleep_interval)

        logger.info("Snipe timed out after %d attempts (%ds)", attempt, target.snipe_timeout)
        return best_result

    async def _try_book_slot(
        self, slot: Slot, day: date, party_size: int
    ) -> BookingResult:
        """Attempt to book a single slot (details → book)."""
        token = await self.get_booking_token(slot, day, party_size)
        return await self.book(token)
