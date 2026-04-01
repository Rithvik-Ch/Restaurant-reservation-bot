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
import time as _time
from datetime import date, datetime, time, timedelta

import httpx
import orjson

from resbot.models import BookingResult, ReservationTarget, Slot, UserProfile
from resbot.platforms.base import ReservationPlatform

logger = logging.getLogger(__name__)

BASE_URL = "https://api.resy.com"

_BROWSER_HEADERS = {
    "Accept": "application/json",
    "Cache-Control": "no-cache",
    "Origin": "https://resy.com",
    "Referer": "https://resy.com/",
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
}


def _print(msg: str) -> None:
    """Print to stderr so the user always sees it, even without -v."""
    print(msg, file=sys.stderr, flush=True)


class ResyClient(ReservationPlatform):
    """High-performance Resy API client with connection pooling and HTTP/2."""

    def __init__(self, profile: UserProfile):
        self._profile = profile
        self._payment_method_id = profile.resy_payment_method_id
        # Pre-compute payment method ID as int for booking
        try:
            self._pm_id_int = int(self._payment_method_id)
        except (ValueError, TypeError):
            self._pm_id_int = self._payment_method_id
        # Pre-serialize payment method JSON for booking (avoids per-request serialization)
        self._payment_json = orjson.dumps({"id": self._pm_id_int}).decode()
        self._session = httpx.AsyncClient(
            base_url=BASE_URL,
            http2=True,
            timeout=httpx.Timeout(5.0, connect=2.0),
            headers={
                "Authorization": f'ResyAPI api_key="{profile.resy_api_key}"',
                "X-Resy-Auth-Token": profile.resy_auth_token,
                **_BROWSER_HEADERS,
            },
            limits=httpx.Limits(
                max_connections=30,
                max_keepalive_connections=15,
                keepalive_expiry=60,
            ),
        )

    @staticmethod
    async def login(email: str, password: str, api_key: str = "") -> dict:
        """Log in to Resy with email/password and return credentials."""
        if not api_key:
            api_key = "VbWk7s3L4KiK5fzlO7JD3Q5EYolJI7n5"
        async with httpx.AsyncClient(
            base_url=BASE_URL,
            http2=True,
            timeout=httpx.Timeout(10.0),
            headers={
                "Authorization": f'ResyAPI api_key="{api_key}"',
                **_BROWSER_HEADERS,
            },
        ) as client:
            resp = await client.post(
                "/3/auth/password",
                json={"email": email, "password": password},
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
                self._pm_id_int = int(self._payment_method_id)
                self._payment_json = orjson.dumps({"id": self._pm_id_int}).decode()

    async def search_venues(self, query: str) -> list[dict]:
        """Search Resy for venues matching query."""
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

    # ── Slot finding ──

    async def find_slots(
        self, venue_id: str, day: date, party_size: int
    ) -> tuple[list[Slot], dict]:
        """Find available slots. Tries /4/find, falls back to /4/venue/calendar."""
        slots, data = await self._find_via_find(venue_id, day, party_size)
        if slots or (data and data.get("results", {}).get("venues")):
            return slots, data
        _print("[find] /4/find failed, trying /4/venue/calendar...")
        return await self._find_via_calendar(venue_id, day, party_size)

    async def find_slots_fast(
        self, venue_id: str, day: date, party_size: int
    ) -> list[Slot]:
        """Speed-optimized find: returns slots only, no raw data, no fallback."""
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
        except httpx.HTTPStatusError:
            return []
        data = orjson.loads(resp.content)
        slots = []
        for venue in data.get("results", {}).get("venues", []):
            for sd in venue.get("slots", []):
                s = self._parse_slot(sd, day)
                if s:
                    slots.append(s)
        return slots

    async def _find_via_find(
        self, venue_id: str, day: date, party_size: int
    ) -> tuple[list[Slot], dict]:
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
        return self._extract_slots(data, day), data

    async def _find_via_calendar(
        self, venue_id: str, day: date, party_size: int
    ) -> tuple[list[Slot], dict]:
        range_end = day + timedelta(days=365)
        try:
            resp = await self._session.get(
                "/4/venue/calendar",
                params={
                    "venue_id": venue_id,
                    "num_seats": party_size,
                    "start_date": day.isoformat(),
                    "end_date": range_end.isoformat(),
                },
            )
            resp.raise_for_status()
        except httpx.HTTPStatusError as e:
            _print(f"[find] /4/venue/calendar returned {e.response.status_code}")
            return [], {}

        data = orjson.loads(resp.content)
        scheduled = data.get("scheduled", [])
        if not scheduled:
            return [], data

        day_str = day.isoformat()
        target_entry = None
        for entry in scheduled:
            if entry.get("date") == day_str:
                target_entry = entry
                break

        if not target_entry:
            _print(f"[find] Date {day} not in calendar")
            return [], data

        if target_entry.get("inventory", {}).get("reservation") != "available":
            _print(f"[find] Date {day} not available")
            return [], data

        _print(f"[find] Date {day} available, fetching slots...")
        try:
            resp2 = await self._session.get(
                "/4/find",
                params={"venue_id": venue_id, "day": day_str, "party_size": party_size, "lat": 0, "long": 0},
            )
            resp2.raise_for_status()
            find_data = orjson.loads(resp2.content)
            slots = self._extract_slots(find_data, day)
            if slots:
                return slots, find_data
        except httpx.HTTPStatusError:
            pass
        return [], data

    def _extract_slots(self, data: dict, day: date) -> list[Slot]:
        slots = []
        for venue in data.get("results", {}).get("venues", []):
            for sd in venue.get("slots", []):
                s = self._parse_slot(sd, day)
                if s:
                    slots.append(s)
        if not slots:
            for sd in data.get("results", {}).get("slots", []):
                s = self._parse_slot(sd, day)
                if s:
                    slots.append(s)
        return slots

    @staticmethod
    def _parse_slot(slot_data: dict, day: date) -> Slot | None:
        config = slot_data.get("config", {})
        start_str = slot_data.get("date", {}).get("start", "")
        if not start_str:
            return None
        try:
            slot_time = datetime.fromisoformat(start_str).time()
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

    # ── Booking ──

    async def get_booking_token(
        self, slot: Slot, day: date, party_size: int
    ) -> str:
        """Get booking token. Tries JSON then form-encoded."""
        payload = {
            "config_id": slot.config_token,
            "day": day.isoformat(),
            "party_size": party_size,
        }
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
        """Execute booking. Uses form-encoded (JSON mangles book_token)."""
        resp = await self._session.post(
            "/3/book",
            data={
                "book_token": booking_token,
                "struct_payment_method": self._payment_json,
                "source_id": "resy.com-venue-details",
            },
        )
        resp.raise_for_status()
        data = orjson.loads(resp.content)
        return BookingResult(
            target_id="",
            success=True,
            reservation_id=str(data.get("reservation_id", "")),
            confirmation_token=data.get("resy_token", ""),
            booked_time=datetime.now(),
        )

    async def warmup(self) -> None:
        """Warm the HTTP/2 connection pool before snipe time."""
        try:
            await self._session.get("/2/user")
        except Exception:
            pass

    async def close(self) -> None:
        await self._session.aclose()

    # ── Snipe ──

    async def snipe(self, target: ReservationTarget, day: date) -> BookingResult:
        """Speed-optimized snipe with concurrent polling.

        Instead of sequential find→sleep→find, fires multiple overlapping
        find requests so one is always in-flight. Zero delay on empty results
        during the critical first seconds after drop. Only backs off on
        rate-limit responses (429/412).
        """
        from resbot.engine import rank_slots

        deadline = _time.monotonic() + target.snipe_timeout
        attempt = 0
        # Concurrent find requests: fire this many in parallel
        concurrency = max(1, min(int(target.snipe_rate // 3), 5))
        # Create a faster session for snipe with tighter timeout
        snipe_timeout = httpx.Timeout(3.0, connect=1.5)

        _print(f"\n{'='*60}")
        _print(f"[snipe] STARTING — {target.venue_name}")
        _print(f"[snipe] venue={target.venue_id} date={day} party={target.party_size}")
        _print(f"[snipe] rate={target.snipe_rate}/s concurrency={concurrency} timeout={target.snipe_timeout}s")
        _print(f"{'='*60}")

        # Pre-build the find params once (avoid per-request dict creation)
        find_params = {
            "venue_id": target.venue_id,
            "day": day.isoformat(),
            "party_size": target.party_size,
            "lat": 0,
            "long": 0,
        }

        # Signal to stop all concurrent tasks on success
        success_result: BookingResult | None = None

        async def _poll_once() -> list[Slot]:
            """Single find request with snipe-optimized timeout."""
            try:
                resp = await self._session.get(
                    "/4/find", params=find_params, timeout=snipe_timeout
                )
                resp.raise_for_status()
            except httpx.HTTPStatusError as e:
                code = e.response.status_code
                if code in (429, 412):
                    # Rate limited — back off briefly
                    await asyncio.sleep(0.5)
                return []
            except (httpx.TimeoutException, httpx.ConnectError):
                return []
            data = orjson.loads(resp.content)
            slots = []
            for venue in data.get("results", {}).get("venues", []):
                for sd in venue.get("slots", []):
                    s = self._parse_slot(sd, day)
                    if s:
                        slots.append(s)
            return slots

        # Phase 1: Burst polling — fire concurrent finds with no delay
        # For the first 10 seconds, be maximally aggressive
        burst_end = _time.monotonic() + min(10.0, target.snipe_timeout)
        sleep_interval = 1.0 / target.snipe_rate

        while _time.monotonic() < deadline:
            if success_result:
                return success_result

            attempt += 1
            in_burst = _time.monotonic() < burst_end

            try:
                # Fire concurrent find requests
                if in_burst and concurrency > 1:
                    poll_tasks = [_poll_once() for _ in range(concurrency)]
                    all_slots_lists = await asyncio.gather(*poll_tasks, return_exceptions=True)
                    # Merge results from all concurrent finds
                    slots = []
                    seen_tokens = set()
                    for sl in all_slots_lists:
                        if isinstance(sl, list):
                            for s in sl:
                                if s.config_token not in seen_tokens:
                                    seen_tokens.add(s.config_token)
                                    slots.append(s)
                else:
                    slots = await _poll_once()

                if attempt <= 3 or attempt % 20 == 0:
                    elapsed = target.snipe_timeout - (deadline - _time.monotonic())
                    mode = "BURST" if in_burst else "poll"
                    _print(f"[snipe] #{attempt} ({elapsed:.0f}s) [{mode}]: {len(slots)} slot(s)")

                ranked = rank_slots(slots, target)
                if not ranked:
                    # In burst mode, don't sleep — go again immediately
                    if not in_burst:
                        await asyncio.sleep(sleep_interval)
                    continue

                # Try top 3 slots in parallel for speed
                top = ranked[:3]
                _print(f"[snipe] Booking: {', '.join(s.slot_time.strftime('%H:%M') for s in top)}")
                tasks = [self._try_book_slot(s, day, target.party_size) for s in top]
                results = await asyncio.gather(*tasks, return_exceptions=True)

                for i, result in enumerate(results):
                    if isinstance(result, BookingResult) and result.success:
                        result.target_id = target.id
                        _print(f"\n{'='*60}")
                        _print(f"  *** RESERVATION CONFIRMED ***")
                        _print(f"  Restaurant: {target.venue_name}")
                        _print(f"  Time:       {top[i].slot_time.strftime('%H:%M')}")
                        _print(f"  Date:       {day}")
                        _print(f"  Party:      {target.party_size}")
                        _print(f"  Confirm:    {result.confirmation_token}")
                        _print(f"  Attempt:    #{attempt}")
                        _print(f"{'='*60}\n")
                        return result  # ← IMMEDIATE STOP
                    elif isinstance(result, Exception):
                        if attempt <= 5:
                            _print(f"[snipe] {top[i].slot_time.strftime('%H:%M')} failed: {result}")

                # Brief pause before retry to avoid hammering a taken slot
                await asyncio.sleep(sleep_interval)

            except httpx.HTTPStatusError as e:
                if e.response.status_code in (429, 412):
                    _print(f"[snipe] Rate limited ({e.response.status_code}), backing off...")
                    await asyncio.sleep(1.0)
                else:
                    _print(f"[snipe] HTTP {e.response.status_code}: {e.response.text[:150]}")
                    await asyncio.sleep(sleep_interval)
            except Exception as e:
                _print(f"[snipe] Error #{attempt}: {type(e).__name__}: {e}")
                await asyncio.sleep(sleep_interval)

        _print(f"\n[snipe] TIMED OUT after {attempt} attempts ({target.snipe_timeout}s)")
        return BookingResult(target_id=target.id, success=False, error="No slots found")

    async def _try_book_slot(
        self, slot: Slot, day: date, party_size: int
    ) -> BookingResult:
        token = await self.get_booking_token(slot, day, party_size)
        return await self.book(token)
