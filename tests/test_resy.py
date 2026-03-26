"""Tests for the Resy API client with mocked HTTP responses."""

from datetime import date, time

import httpx
import pytest
import respx

from resbot.models import Slot, UserProfile
from resbot.platforms.resy import ResyClient


@pytest.fixture
def profile():
    return UserProfile(
        name="Test User",
        phone="+15551234567",
        email="test@example.com",
        resy_api_key="test-api-key",
        resy_auth_token="test-auth-token",
        resy_payment_method_id="pm-123",
    )


@pytest.fixture
def client(profile):
    return ResyClient(profile)


@respx.mock
@pytest.mark.asyncio
async def test_authenticate(client):
    respx.get("https://api.resy.com/2/user").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": 12345,
                "payment_methods": [{"id": 67890}],
            },
        )
    )
    await client.authenticate(client._profile)
    assert client._payment_method_id == "pm-123"


@respx.mock
@pytest.mark.asyncio
async def test_authenticate_auto_detect_payment(profile):
    profile.resy_payment_method_id = None
    c = ResyClient(profile)
    respx.get("https://api.resy.com/2/user").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": 12345,
                "payment_methods": [{"id": 99999}],
            },
        )
    )
    await c.authenticate(profile)
    assert c._payment_method_id == "99999"
    await c.close()


@respx.mock
@pytest.mark.asyncio
async def test_find_slots(client):
    respx.get("https://api.resy.com/4/find").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": {
                    "venues": [
                        {
                            "slots": [
                                {
                                    "config": {"token": "config-1", "type": "Dining Room"},
                                    "date": {"start": "2025-06-15 19:00:00"},
                                    "shift": {"label": "Dinner"},
                                    "payment": {},
                                },
                                {
                                    "config": {"token": "config-2", "type": "Bar"},
                                    "date": {"start": "2025-06-15 20:30:00"},
                                    "shift": {"label": "Dinner"},
                                    "payment": {"is_paid": True},
                                },
                            ]
                        }
                    ]
                }
            },
        )
    )
    slots = await client.find_slots("123", date(2025, 6, 15), 2)
    assert len(slots) == 2
    assert slots[0].config_token == "config-1"
    assert slots[0].slot_time == time(19, 0)
    assert slots[0].table_type == "Dining Room"
    assert slots[1].payment_required is True
    await client.close()


@respx.mock
@pytest.mark.asyncio
async def test_get_booking_token(client):
    respx.post("https://api.resy.com/3/details").mock(
        return_value=httpx.Response(
            200,
            json={"book_token": {"value": "book-token-abc"}},
        )
    )
    slot = Slot(
        config_token="config-1",
        slot_time=time(19, 0),
        date=date(2025, 6, 15),
    )
    token = await client.get_booking_token(slot, date(2025, 6, 15), 2)
    assert token == "book-token-abc"
    await client.close()


@respx.mock
@pytest.mark.asyncio
async def test_book(client):
    respx.post("https://api.resy.com/3/book").mock(
        return_value=httpx.Response(
            200,
            json={
                "resy_token": "RESY-CONF-123",
                "reservation_id": 456789,
            },
        )
    )
    result = await client.book("book-token-abc")
    assert result.success is True
    assert result.confirmation_token == "RESY-CONF-123"
    assert result.reservation_id == "456789"
    await client.close()


@respx.mock
@pytest.mark.asyncio
async def test_search_venues(client):
    respx.get("https://api.resy.com/3/venuesearch/search").mock(
        return_value=httpx.Response(
            200,
            json={
                "search": {
                    "hits": [
                        {
                            "id": {"resy": 58432},
                            "name": "Test Restaurant",
                            "location": {"name": "New York"},
                            "cuisine": ["Italian"],
                            "price_range_id": 3,
                            "url_slug": "test-restaurant",
                        }
                    ]
                }
            },
        )
    )
    results = await client.search_venues("Test")
    assert len(results) == 1
    assert results[0]["name"] == "Test Restaurant"
    assert results[0]["venue_id"] == "58432"
    await client.close()


@respx.mock
@pytest.mark.asyncio
async def test_warmup(client):
    respx.get("https://api.resy.com/2/user").mock(
        return_value=httpx.Response(200, json={})
    )
    await client.warmup()
    await client.close()
