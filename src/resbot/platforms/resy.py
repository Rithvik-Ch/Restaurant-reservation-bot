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
import sys
from datetime import date, datetime, time

import httpx
import orjson

from resbot.models import BookingResult, ReservationTarget, Slot, UserProfile
from resbot.platforms.base import ReservationPlatform

logger = logging.getLogger(__name__)

BASE_URL = "https://api.resy.com"


def _print(msg: str) -> None:
    """Print to stderr so the user always sees it, even without -v."""
    print(msg, file=sys.stderr, flush=True)


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
                "Origin": "https://resy.com",
                "Referer": "https://resy.com/",
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
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
                _print(f"[auth] Auto-detected payment method: {self._payment_method_id}")

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
    ) -> tuple[list[Slot], dict]:
        """Find available slots. Returns (slots, raw_response_dict).

        Tries /4/find first. If it returns 500 (some venues don't support it),
        falls back to /4/venue/calendar to check availability.
        """
        from datetime import date as _date
        if day < _date.today():
            _print(f"[find] WARNING: date {day} is in the past! Resy will reject this.")

        # Try /4/find first
        slots, data = await self._find_via_find(venue_id, day, party_size)
        if slots or (data and data.get("results", {}).get("venues")):
            return slots, data

        # Fall back to /4/venue/calendar
        _print("[find] /4/find failed, trying /4/venue/calendar...")
        return await self._find_via_calendar(venue_id, day, party_size)

    async def _find_via_find(
        self, venue_id: str, day: date, party_size: int
    ) -> tuple[list[Slot], dict]:
        """Try the standard /4/find endpoint."""
        try:
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
        except httpx.HTTPStatusError as e:
            _print(f"[find] /4/find returned {e.response.status_code} for venue={venue_id}")
            return [], {}

        data = orjson.loads(resp.content)
        slots = self._extract_slots_from_find(data, day)
        return slots, data

    async def _find_via_calendar(
        self, venue_id: str, day: date, party_size: int
    ) -> tuple[list[Slot], dict]:
        """Try /4/venue/calendar endpoint, then fetch slots for available dates."""
        from datetime import timedelta
        # Calendar API needs a wide date range (Resy website uses ~1 year)
        range_start = day
        range_end = day + timedelta(days=365)
        try:
            resp = await self._session.get(
                "/4/venue/calendar",
                params={
                    "venue_id": venue_id,
                    "num_seats": party_size,
                    "start_date": range_start.isoformat(),
                    "end_date": range_end.isoformat(),
                },
            )
            resp.raise_for_status()
        except httpx.HTTPStatusError as e:
            _print(f"[find] /4/venue/calendar returned {e.response.status_code}")
            _print(f"[find] Response: {e.response.text[:500]}")
            return [], {}

        data = orjson.loads(resp.content)
        _print(f"[find] Calendar response keys: {list(data.keys())}")

        scheduled = data.get("scheduled", [])
        if not scheduled:
            _print(f"[find] No scheduled dates in calendar response")
            preview = orjson.dumps(data).decode()[:600]
            _print(f"[find] Preview: {preview}")
            return [], data

        # Find our target date
        day_str = day.isoformat()
        target_entry = None
        for entry in scheduled:
            if entry.get("date") == day_str:
                target_entry = entry
                break

        if not target_entry:
            _print(f"[find] Date {day} not in calendar. Available dates:")
            for entry in scheduled[:10]:
                inv = entry.get("inventory", {})
                status = inv.get("reservation", "unknown")
                _print(f"  {entry.get('date')}: {status}")
            return [], data

        inv = target_entry.get("inventory", {})
        res_status = inv.get("reservation", "unknown")
        if res_status != "available":
            _print(f"[find] Date {day} reservation status: {res_status}")
            return [], data

        _print(f"[find] Date {day} is available! Fetching slot details...")

        # Date is available — use /4/find with this specific date
        # (some venues support /4/find only for dates confirmed by calendar)
        try:
            resp2 = await self._session.get(
                "/4/find",
                params={
                    "venue_id": venue_id,
                    "day": day_str,
                    "party_size": party_size,
                    "lat": 0,
                    "long": 0,
                },
            )
            resp2.raise_for_status()
            find_data = orjson.loads(resp2.content)
            slots = self._extract_slots_from_find(find_data, day)
            if slots:
                _print(f"[find] Got {len(slots)} slot(s) via calendar→find")
                return slots, find_data
        except httpx.HTTPStatusError:
            pass

        _print("[find] Calendar confirms availability but can't fetch individual slots")
        _print("[find] This venue may require a different booking flow")
        return [], data

    def _extract_slots_from_find(self, data: dict, day: date) -> list[Slot]:
        """Extract slots from a /4/find response."""
        slots = []
        for venue in data.get("results", {}).get("venues", []):
            for slot_data in venue.get("slots", []):
                slot = self._parse_slot(slot_data, day)
                if slot:
                    slots.append(slot)
        # Alternate path
        if not slots:
            for slot_data in data.get("results", {}).get("slots", []):
                slot = self._parse_slot(slot_data, day)
                if slot:
                    slots.append(slot)
        return slots

    @staticmethod
    def _parse_slot(slot_data: dict, day: date) -> Slot | None:
        """Parse a single slot from API response. Returns None on failure."""
        config = slot_data.get("config", {})
        dt = slot_data.get("date", {})
        start_str = dt.get("start", "")
        if not start_str:
            return None
        try:
            slot_dt = datetime.fromisoformat(start_str)
            slot_time = slot_dt.time()
        except (ValueError, TypeError):
            return None
        return Slot(
            config_token=config.get("token", ""),
            slot_time=slot_time,
            date=day,
            table_type=config.get("type", ""),
            shift_label=slot_data.get("shift", {}).get("label", ""),
            payment_required=bool(slot_data.get("payment", {}).get("is_paid")),
        )

    async def get_booking_token(
        self, slot: Slot, day: date, party_size: int
    ) -> str:
        """Get booking token from a slot config token."""
        payload = {
            "config_id": slot.config_token,
            "day": day.isoformat(),
            "party_size": party_size,
        }
        # Try JSON first, fall back to form-encoded
        try:
            resp = await self._session.post("/3/details", json=payload)
            resp.raise_for_status()
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 415:
                resp = await self._session.post("/3/details", data=payload)
                resp.raise_for_status()
            else:
                raise
        data = orjson.loads(resp.content)
        book_token = data.get("book_token", {}).get("value", "")
        if not book_token:
            raise ValueError("No booking token in response")
        return book_token

    async def book(self, booking_token: str) -> BookingResult:
        """Execute booking with pre-obtained token."""
        pm_id = self._payment_method_id
        try:
            pm_id_int = int(pm_id)
        except (ValueError, TypeError):
            pm_id_int = pm_id

        # Form-encoded with struct_payment_method as JSON string
        # (Resy rejects JSON bodies for /3/book — book_token gets mangled)
        payload = {
            "book_token": booking_token,
            "struct_payment_method": orjson.dumps({"id": pm_id_int}).decode(),
            "source_id": "resy.com-venue-details",
        }
        resp = await self._session.post("/3/book", data=payload)
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

        1. First does an immediate check — if slots are already open, books right away.
        2. If no slots yet, enters burst polling loop at target.snipe_rate until
           target.snipe_timeout expires.
        """
        import time as _time

        from resbot.engine import rank_slots

        best_result = BookingResult(
            target_id=target.id, success=False, error="No slots found"
        )

        sleep_interval = 1.0 / target.snipe_rate
        deadline = _time.monotonic() + target.snipe_timeout
        attempt = 0

        _print(f"[snipe] Starting: venue={target.venue_id} date={day} party={target.party_size} rate={target.snipe_rate}/s timeout={target.snipe_timeout}s")

        while _time.monotonic() < deadline:
            attempt += 1
            try:
                slots, raw_data = await self.find_slots(target.venue_id, day, target.party_size)

                # Always print first few attempts and then every 10th
                if attempt <= 3 or attempt % 10 == 0:
                    _print(f"[snipe] Attempt {attempt}: {len(slots)} raw slot(s) from API")

                # On first attempt with no slots, dump diagnostics
                if attempt == 1 and not slots:
                    results_keys = list(raw_data.get("results", {}).keys())
                    venues = raw_data.get("results", {}).get("venues", [])
                    _print(f"[snipe] API response results keys: {results_keys}")
                    _print(f"[snipe] Venues in response: {len(venues)}")
                    if venues:
                        v = venues[0]
                        _print(f"[snipe] First venue keys: {list(v.keys())}")
                        _print(f"[snipe] First venue slots count: {len(v.get('slots', []))}")
                    # Show a compact preview of the raw JSON
                    raw_str = orjson.dumps(raw_data).decode()
                    preview = raw_str[:800]
                    _print(f"[snipe] Raw response preview: {preview}")

                ranked = rank_slots(slots, target)

                if attempt <= 3 or attempt % 10 == 0:
                    if slots:
                        slot_times = ", ".join(s.slot_time.strftime("%H:%M") for s in slots[:8])
                        _print(f"[snipe]   Slot times: {slot_times}")
                        _print(f"[snipe]   After ranking: {len(ranked)} slot(s)")

                if not ranked:
                    await asyncio.sleep(sleep_interval)
                    continue

                # Try top 3 slots in parallel
                top_slots = ranked[:3]
                _print(f"[snipe] Booking top {len(top_slots)} slot(s): {', '.join(s.slot_time.strftime('%H:%M') for s in top_slots)}")
                tasks = [
                    self._try_book_slot(slot, day, target.party_size)
                    for slot in top_slots
                ]
                results = await asyncio.gather(*tasks, return_exceptions=True)

                for i, result in enumerate(results):
                    if isinstance(result, BookingResult) and result.success:
                        result.target_id = target.id
                        _print(f"[snipe] SUCCESS on attempt {attempt}! Confirmation: {result.confirmation_token}")
                        return result
                    elif isinstance(result, Exception):
                        _print(f"[snipe] Slot {top_slots[i].slot_time.strftime('%H:%M')} booking error: {result}")

                best_result = BookingResult(
                    target_id=target.id,
                    success=False,
                    error="Slots found but booking failed",
                )
                await asyncio.sleep(sleep_interval)
            except httpx.HTTPStatusError as e:
                _print(f"[snipe] HTTP {e.response.status_code} on attempt {attempt}: {e.response.text[:200]}")
                if e.response.status_code == 412:
                    await asyncio.sleep(sleep_interval)
                    continue
                await asyncio.sleep(sleep_interval)
            except Exception as e:
                _print(f"[snipe] Error on attempt {attempt}: {type(e).__name__}: {e}")
                await asyncio.sleep(sleep_interval)

        _print(f"[snipe] Timed out after {attempt} attempts ({target.snipe_timeout}s)")
        return best_result

    async def _try_book_slot(
        self, slot: Slot, day: date, party_size: int
    ) -> BookingResult:
        """Attempt to book a single slot (details → book)."""
        token = await self.get_booking_token(slot, day, party_size)
        return await self.book(token)
