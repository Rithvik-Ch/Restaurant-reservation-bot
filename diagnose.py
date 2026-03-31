#!/usr/bin/env python3
"""Standalone diagnostic script - run this to test your Resy connection.

Usage: python3 diagnose.py
"""

import asyncio
import json
import sys
from pathlib import Path

# Add src/ to path
sys.path.insert(0, str(Path(__file__).parent / "src"))


async def main():
    print("=" * 60)
    print("resbot Resy API Diagnostic")
    print("=" * 60)

    # Load profile
    try:
        from resbot.config import load_profile
        profile = load_profile()
    except Exception as e:
        print(f"\n[FAIL] Could not load profile: {e}")
        print("Run: python3 run.py profile setup")
        return

    print(f"\n[1] Profile loaded:")
    print(f"    API key:    {profile.resy_api_key[:12]}..." if len(profile.resy_api_key) > 12 else f"    API key:    {profile.resy_api_key!r}")
    print(f"    Auth token: {profile.resy_auth_token[:12]}..." if len(profile.resy_auth_token) > 12 else f"    Auth token: {profile.resy_auth_token!r}")
    print(f"    Payment ID: {profile.resy_payment_method_id or '(not set)'}")

    if not profile.resy_api_key:
        print("\n[FAIL] No API key set! Run: python3 run.py profile setup")
        return
    if not profile.resy_auth_token:
        print("\n[FAIL] No auth token set! Run: python3 run.py profile setup")
        return

    # Test auth
    import httpx

    headers = {
        "Authorization": f'ResyAPI api_key="{profile.resy_api_key}"',
        "X-Resy-Auth-Token": profile.resy_auth_token,
        "Accept": "application/json",
    }

    async with httpx.AsyncClient(
        base_url="https://api.resy.com",
        http2=True,
        timeout=httpx.Timeout(10.0),
        headers=headers,
    ) as client:

        # Step 1: Test auth token with /2/user
        print(f"\n[2] Testing auth token (GET /2/user)...")
        try:
            resp = await client.get("/2/user")
            print(f"    Status: {resp.status_code}")
            if resp.status_code == 200:
                data = resp.json()
                print(f"    User: {data.get('first_name', '?')} {data.get('last_name', '?')}")
                print(f"    Email: {data.get('em_address', '?')}")
                pm = data.get("payment_methods", [])
                if pm:
                    print(f"    Payment methods: {[p.get('id') for p in pm]}")
                else:
                    print("    Payment methods: NONE (you need one to book)")
                print("    [OK] Auth token is VALID")
            elif resp.status_code == 419:
                print("    [FAIL] Auth token is EXPIRED. Get a fresh one from browser.")
                print("    Go to resy.com > F12 > Network > click api.resy.com request")
                print("    Copy x-resy-auth-token from Request Headers")
                return
            else:
                print(f"    [FAIL] Unexpected status. Body: {resp.text[:300]}")
                return
        except Exception as e:
            print(f"    [FAIL] Could not connect: {e}")
            return

        # Step 2: Test venue lookup
        venue_id = input("\n    Enter venue ID to test (e.g. 90333): ").strip()
        if not venue_id:
            print("    Skipping venue test.")
            return

        test_date = input("    Enter date to test (YYYY-MM-DD, e.g. 2026-04-09): ").strip()
        if not test_date:
            from datetime import date, timedelta
            test_date = (date.today() + timedelta(days=7)).isoformat()
            print(f"    Using default: {test_date}")

        party = input("    Party size (default 2): ").strip() or "2"

        print(f"\n[3] Testing slot search (GET /4/find)...")
        print(f"    venue_id={venue_id} day={test_date} party_size={party}")
        try:
            resp = await client.get(
                "/4/find",
                params={
                    "venue_id": venue_id,
                    "day": test_date,
                    "party_size": int(party),
                    "lat": 0,
                    "long": 0,
                },
            )
            print(f"    Status: {resp.status_code}")
            print(f"    Response length: {len(resp.text)} bytes")

            if resp.status_code == 200:
                data = resp.json()
                print(f"    Top-level keys: {list(data.keys())}")
                results = data.get("results", {})
                print(f"    results keys: {list(results.keys())}")
                venues = results.get("venues", [])
                print(f"    Venue count: {len(venues)}")

                total_slots = 0
                for i, v in enumerate(venues):
                    slots = v.get("slots", [])
                    total_slots += len(slots)
                    print(f"    Venue[{i}] keys: {list(v.keys())}")
                    print(f"    Venue[{i}] slots: {len(slots)}")
                    for s in slots[:5]:
                        start = s.get("date", {}).get("start", "?")
                        stype = s.get("config", {}).get("type", "?")
                        print(f"      - {start}  type={stype!r}")
                    if len(slots) > 5:
                        print(f"      ... and {len(slots) - 5} more")

                if total_slots == 0:
                    print(f"\n    [WARN] 0 slots returned. This could mean:")
                    print(f"    - No availability on {test_date}")
                    print(f"    - Reservations haven't opened yet for this date")
                    print(f"    - Venue ID {venue_id} is incorrect")
                    print(f"\n    Raw response (first 1000 chars):")
                    print(f"    {resp.text[:1000]}")
                else:
                    print(f"\n    [OK] Found {total_slots} slot(s)! API is working correctly.")

            elif resp.status_code == 500:
                print(f"    [FAIL] 500 from /4/find — this venue may use a different API")
                print(f"    Trying /4/venue/calendar instead...")

                try:
                    resp2 = await client.get(
                        "/4/venue/calendar",
                        params={
                            "venue_id": venue_id,
                            "num_seats": int(party),
                            "start_date": test_date,
                            "end_date": test_date,
                        },
                    )
                    print(f"\n[4] /4/venue/calendar response:")
                    print(f"    Status: {resp2.status_code}")

                    if resp2.status_code == 200:
                        cal = resp2.json()
                        print(f"    Keys: {list(cal.keys())}")
                        scheduled = cal.get("scheduled", [])
                        print(f"    Scheduled entries: {len(scheduled)}")
                        for entry in scheduled[:10]:
                            d = entry.get("date", "?")
                            inv = entry.get("inventory", {})
                            res_status = inv.get("reservation", "unknown")
                            print(f"      {d}: {res_status}")
                        if scheduled:
                            print(f"\n    [OK] Calendar endpoint works for this venue!")
                        else:
                            print(f"\n    Calendar returned no dates.")
                            print(f"    Raw: {resp2.text[:800]}")
                    else:
                        print(f"    Calendar also failed: {resp2.text[:300]}")
                except Exception as e2:
                    print(f"    Calendar request failed: {e2}")
            else:
                print(f"    [FAIL] Unexpected status {resp.status_code}")
                print(f"    Body: {resp.text[:500]}")

        except Exception as e:
            print(f"    [FAIL] Request error: {e}")

    print("\n" + "=" * 60)
    print("Diagnostic complete.")


if __name__ == "__main__":
    asyncio.run(main())
