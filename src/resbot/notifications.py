"""Notification system for booking results.

Supports webhook (HTTP POST) notifications. Easily extensible
for email, SMS, or other channels.
"""

from __future__ import annotations

import logging
from datetime import datetime

import httpx
import orjson

from resbot.models import BookingResult

logger = logging.getLogger(__name__)


class WebhookNotifier:
    """Send booking results to a webhook URL (Slack, Discord, etc.)."""

    def __init__(self, webhook_url: str):
        self._url = webhook_url
        self._client = httpx.AsyncClient(timeout=10)

    async def notify(self, result: BookingResult, venue_name: str = "") -> None:
        if result.success:
            text = (
                f"Reservation booked! {venue_name}\n"
                f"Confirmation: {result.confirmation_token}\n"
                f"Time: {result.booked_time}"
            )
        else:
            text = (
                f"Reservation failed: {venue_name}\n"
                f"Error: {result.error}"
            )

        payload = {"text": text}
        try:
            resp = await self._client.post(
                self._url,
                content=orjson.dumps(payload),
                headers={"Content-Type": "application/json"},
            )
            resp.raise_for_status()
            logger.info("Notification sent to webhook")
        except Exception as e:
            logger.warning("Failed to send notification: %s", e)

    async def close(self) -> None:
        await self._client.aclose()


class ConsoleNotifier:
    """Print booking results to stdout."""

    async def notify(self, result: BookingResult, venue_name: str = "") -> None:
        timestamp = datetime.now().strftime("%H:%M:%S")
        if result.success:
            print(f"[{timestamp}] BOOKED: {venue_name} - {result.confirmation_token}")
        else:
            print(f"[{timestamp}] FAILED: {venue_name} - {result.error}")

    async def close(self) -> None:
        pass
