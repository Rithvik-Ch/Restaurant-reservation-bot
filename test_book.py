#!/usr/bin/env python3
"""Test the full booking flow with verbose output.

Usage: python3 test_book.py VENUE_ID DATE PARTY_SIZE
Example: python3 test_book.py 90333 2026-04-01 2
"""

import asyncio
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))


async def main():
    if len(sys.argv) < 4:
        print("Usage: python3 test_book.py VENUE_ID DATE PARTY_SIZE")
        print("Example: python3 test_book.py 90333 2026-04-01 2")
        return

    venue_id = sys.argv[1]
    day = date.fromisoformat(sys.argv[2])
    party_size = int(sys.argv[3])

    import httpx
    import orjson
    from resbot.config import load_profile

    profile = load_profile()
    print(f"Profile: {profile.name}")
    print(f"Payment methods: {profile.resy_payment_method_id}")

    headers = {
        "Authorization": f'ResyAPI api_key="{profile.resy_api_key}"',
        "X-Resy-Auth-Token": profile.resy_auth_token,
        "Accept": "application/json",
        "Origin": "https://resy.com",
        "Referer": "https://resy.com/",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    }

    async with httpx.AsyncClient(
        base_url="https://api.resy.com",
        http2=True,
        timeout=httpx.Timeout(10.0),
        headers=headers,
    ) as client:

        # Step 1: Find slots
        print(f"\n[1] Finding slots for venue={venue_id} date={day} party={party_size}...")
        resp = await client.get("/4/find", params={
            "venue_id": venue_id, "day": day.isoformat(),
            "party_size": party_size, "lat": 0, "long": 0,
        })
        print(f"    Status: {resp.status_code}")
        data = orjson.loads(resp.content)
        venues = data.get("results", {}).get("venues", [])
        if not venues:
            print("    No venues in response")
            return

        slots = venues[0].get("slots", [])
        print(f"    Found {len(slots)} slots")

        # Pick first dinner slot (17:00+)
        chosen = None
        for s in slots:
            start = s.get("date", {}).get("start", "")
            if "19:" in start or "18:" in start or "20:" in start:
                chosen = s
                break
        if not chosen:
            chosen = slots[0]

        config_token = chosen.get("config", {}).get("token", "")
        start_time = chosen.get("date", {}).get("start", "")
        print(f"    Using slot: {start_time} token={config_token[:40]}...")

        # Step 2: Get booking token
        print(f"\n[2] Getting booking token (POST /3/details)...")
        details_payload = {
            "config_id": config_token,
            "day": day.isoformat(),
            "party_size": party_size,
        }
        print(f"    Trying JSON body...")
        resp2 = await client.post("/3/details", json=details_payload)
        print(f"    Status: {resp2.status_code}")
        if resp2.status_code != 200 and resp2.status_code != 201:
            print(f"    Response: {resp2.text[:500]}")
            print(f"    Trying form-encoded...")
            resp2 = await client.post("/3/details", data=details_payload)
            print(f"    Status: {resp2.status_code}")
            if resp2.status_code not in (200, 201):
                print(f"    Response: {resp2.text[:500]}")
                return

        details_data = orjson.loads(resp2.content)
        book_token = details_data.get("book_token", {}).get("value", "")
        print(f"    Book token: {book_token[:40]}...")
        print(f"    Details response keys: {list(details_data.keys())}")

        # Show payment info from details
        payment_info = details_data.get("payment", {})
        print(f"    Payment info from details: {orjson.dumps(payment_info).decode()[:300]}")

        pm_id = profile.resy_payment_method_id
        try:
            pm_id_int = int(pm_id)
        except (ValueError, TypeError):
            pm_id_int = pm_id

        # Step 3: Try booking with different payload formats
        print(f"\n[3] Attempting to book (POST /3/book)...")
        print(f"    Payment method ID: {pm_id} (int: {pm_id_int})")

        # Format A: JSON body, nested payment object
        print(f"\n    --- Format A: JSON + nested object ---")
        payload_a = {
            "book_token": book_token,
            "struct_payment_method": {"id": pm_id_int},
            "source_id": "resy.com-venue-details",
        }
        print(f"    Payload: {orjson.dumps(payload_a).decode()[:300]}")
        resp3a = await client.post("/3/book", json=payload_a)
        print(f"    Status: {resp3a.status_code}")
        print(f"    Response: {resp3a.text[:500]}")
        if resp3a.status_code in (200, 201):
            print("\n    SUCCESS!")
            return

        # Format B: Form-encoded, payment as JSON string
        print(f"\n    --- Format B: Form + JSON string ---")
        payload_b = {
            "book_token": book_token,
            "struct_payment_method": orjson.dumps({"id": pm_id_int}).decode(),
            "source_id": "resy.com-venue-details",
        }
        print(f"    Payload: {payload_b}")
        resp3b = await client.post("/3/book", data=payload_b)
        print(f"    Status: {resp3b.status_code}")
        print(f"    Response: {resp3b.text[:500]}")
        if resp3b.status_code in (200, 201):
            print("\n    SUCCESS!")
            return

        # Format C: JSON body, payment as JSON string
        print(f"\n    --- Format C: JSON + string payment ---")
        payload_c = {
            "book_token": book_token,
            "struct_payment_method": orjson.dumps({"id": pm_id_int}).decode(),
            "source_id": "resy.com-venue-details",
        }
        resp3c = await client.post("/3/book", json=payload_c)
        print(f"    Status: {resp3c.status_code}")
        print(f"    Response: {resp3c.text[:500]}")
        if resp3c.status_code in (200, 201):
            print("\n    SUCCESS!")
            return

        # Format D: Form-encoded, nested payment (stringified)
        print(f"\n    --- Format D: Form + string payment + Content-Type ---")
        payload_d = {
            "book_token": book_token,
            "struct_payment_method": f'{{"id":{pm_id_int}}}',
            "source_id": "resy.com-venue-details",
        }
        custom_headers = dict(headers)
        custom_headers["Content-Type"] = "application/x-www-form-urlencoded"
        resp3d = await client.post("/3/book", data=payload_d, headers=custom_headers)
        print(f"    Status: {resp3d.status_code}")
        print(f"    Response: {resp3d.text[:500]}")
        if resp3d.status_code in (200, 201):
            print("\n    SUCCESS!")
            return

        print("\n    All formats failed. Check the response messages above.")


if __name__ == "__main__":
    asyncio.run(main())
