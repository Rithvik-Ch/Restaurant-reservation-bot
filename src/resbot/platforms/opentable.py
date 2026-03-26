"""OpenTable reservation adapter.

OpenTable's public API supports availability discovery but not programmatic
booking. This adapter uses httpx for slot discovery and Playwright for the
actual booking step (browser automation).

Install with: pip install resbot[opentable]
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

AVAILABILITY_URL = "https://www.opentable.com/dapi/fe/gql"


class OpenTableClient(ReservationPlatform):
    """OpenTable adapter using API for discovery + Playwright for booking."""

    def __init__(self, profile: UserProfile):
        self._profile = profile
        self._session = httpx.AsyncClient(
            timeout=httpx.Timeout(15.0, connect=5.0),
            headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
        )
        self._browser = None
        self._page = None

    async def authenticate(self, profile: UserProfile) -> None:
        """OpenTable auth is handled during the booking step via Playwright."""
        pass

    async def search_venues(self, query: str) -> list[dict]:
        """Search OpenTable for restaurants."""
        resp = await self._session.post(
            AVAILABILITY_URL,
            content=orjson.dumps(
                {
                    "operationName": "Autocomplete",
                    "variables": {"term": query, "latitude": 0, "longitude": 0},
                    "query": """
                        query Autocomplete($term: String!, $latitude: Float, $longitude: Float) {
                            autocomplete(term: $term, latitude: $latitude, longitude: $longitude) {
                                restaurants {
                                    rid
                                    name
                                    locality
                                    cuisine
                                    priceRange
                                }
                            }
                        }
                    """,
                }
            ),
        )
        resp.raise_for_status()
        data = orjson.loads(resp.content)
        results = []
        for r in data.get("data", {}).get("autocomplete", {}).get("restaurants", []):
            results.append(
                {
                    "venue_id": str(r.get("rid", "")),
                    "name": r.get("name", ""),
                    "location": r.get("locality", ""),
                    "cuisine": [r.get("cuisine", "")],
                    "price_range": r.get("priceRange", 0),
                }
            )
        return results

    async def find_slots(
        self, venue_id: str, day: date, party_size: int
    ) -> list[Slot]:
        """Find available reservation slots on OpenTable."""
        resp = await self._session.post(
            AVAILABILITY_URL,
            content=orjson.dumps(
                {
                    "operationName": "RestaurantAvailability",
                    "variables": {
                        "rid": int(venue_id),
                        "date": day.isoformat(),
                        "partySize": party_size,
                    },
                    "query": """
                        query RestaurantAvailability($rid: Int!, $date: String!, $partySize: Int!) {
                            availability(rid: $rid, date: $date, partySize: $partySize) {
                                timeslots {
                                    dateTime
                                    isAvailable
                                    token
                                    tableType
                                }
                            }
                        }
                    """,
                }
            ),
        )
        resp.raise_for_status()
        data = orjson.loads(resp.content)

        slots = []
        for ts in data.get("data", {}).get("availability", {}).get("timeslots", []):
            if not ts.get("isAvailable"):
                continue
            try:
                dt = datetime.fromisoformat(ts["dateTime"])
                slots.append(
                    Slot(
                        config_token=ts.get("token", ""),
                        slot_time=dt.time(),
                        date=day,
                        table_type=ts.get("tableType", ""),
                    )
                )
            except (ValueError, KeyError):
                continue
        return slots

    async def get_booking_token(
        self, slot: Slot, day: date, party_size: int
    ) -> str:
        """For OpenTable, the slot token is used directly."""
        return slot.config_token

    async def book(self, booking_token: str) -> BookingResult:
        """Book via Playwright browser automation.

        This launches a headless browser, navigates to the OpenTable booking
        page with the slot token, fills in user details, and submits.
        """
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            return BookingResult(
                target_id="",
                success=False,
                error="Playwright not installed. Run: pip install resbot[opentable]",
            )

        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                page = await browser.new_page()

                # Navigate to OpenTable booking confirmation page
                booking_url = f"https://www.opentable.com/booking/details?token={booking_token}"
                await page.goto(booking_url, wait_until="networkidle")

                # Fill in guest details
                await page.fill('[name="firstName"]', self._profile.name.split()[0])
                if len(self._profile.name.split()) > 1:
                    await page.fill('[name="lastName"]', self._profile.name.split()[-1])
                await page.fill('[name="phoneNumber"]', self._profile.phone)
                await page.fill('[name="email"]', self._profile.email)

                # Submit the booking
                submit = page.locator('button[type="submit"]')
                await submit.click()

                # Wait for confirmation
                await page.wait_for_url("**/confirmation**", timeout=15000)

                confirmation = await page.text_content(".confirmation-number") or "confirmed"

                await browser.close()

                return BookingResult(
                    target_id="",
                    success=True,
                    confirmation_token=confirmation,
                    booked_time=datetime.now(),
                )
        except Exception as e:
            logger.error("OpenTable booking failed: %s", e)
            return BookingResult(
                target_id="", success=False, error=f"Browser booking failed: {e}"
            )

    async def warmup(self) -> None:
        """Warm the HTTP connection."""
        try:
            await self._session.get("https://www.opentable.com")
        except Exception:
            pass

    async def close(self) -> None:
        """Close the HTTP client."""
        await self._session.aclose()
