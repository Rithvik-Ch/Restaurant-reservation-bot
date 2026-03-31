"""CLI interface for resbot."""

from __future__ import annotations

import asyncio
import logging
import sys
from datetime import date, time
from pathlib import Path

import click
import yaml

from resbot.config import (
    ensure_config_dir,
    load_profile,
    load_targets,
    remove_target,
    save_profile,
    save_target,
)
from resbot.models import MealTime, ReservationTarget, TimeWindow, UserProfile


def setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


@click.group()
@click.option("-v", "--verbose", is_flag=True, help="Enable debug logging")
@click.option(
    "--config-dir",
    type=click.Path(path_type=Path),
    default=None,
    help="Config directory (default: ~/.resbot)",
)
@click.pass_context
def cli(ctx, verbose, config_dir):
    """resbot - Speed-optimized restaurant reservation bot."""
    setup_logging(verbose)
    ctx.ensure_object(dict)
    ctx.obj["config_dir"] = config_dir


# ── Profile commands ──


@cli.group()
def profile():
    """Manage user profile."""


@profile.command("setup")
@click.pass_context
def profile_setup(ctx):
    """Interactive profile configuration."""
    config_dir = ctx.obj["config_dir"]
    ensure_config_dir(config_dir)

    click.echo("=== resbot Profile Setup ===\n")

    name = click.prompt("Your name")
    phone = click.prompt("Phone number (e.g. +15551234567)")
    email = click.prompt("Email")

    click.echo("\n--- Resy Credentials ---")
    click.echo("We'll try to log in automatically first.\n")

    resy_email = click.prompt("Resy email (the one you use to log in at resy.com)")
    resy_password = click.prompt("Resy password", hide_input=True)

    click.echo("\nAttempting auto-login...")

    creds = {}
    try:
        from resbot.platforms.resy import ResyClient

        creds = asyncio.run(ResyClient.login(resy_email, resy_password))
        click.echo(f"Success! Logged in as {creds.get('first_name', '')} {creds.get('last_name', '')}")
    except Exception:
        click.echo("\nAuto-login didn't work (Resy sometimes blocks this).")
        click.echo("No worries — you can grab the credentials from your browser instead.\n")
        click.echo("Here's how:")
        click.echo("  1. Go to resy.com and log in")
        click.echo("  2. Open any restaurant page")
        click.echo("  3. Press F12 (or right-click > Inspect) to open Developer Tools")
        click.echo("  4. Click the 'Network' tab")
        click.echo("  5. Refresh the page")
        click.echo("  6. In the list of requests, click any one that goes to 'api.resy.com'")
        click.echo("  7. Scroll down to 'Request Headers' and find:")
        click.echo('     - Authorization: ResyAPI api_key="YOUR_API_KEY"')
        click.echo("     - x-resy-auth-token: YOUR_AUTH_TOKEN")
        click.echo("  8. Copy those two values below\n")

        resy_api_key = click.prompt("Resy API key (the part inside the quotes after api_key=)")
        resy_auth_token = click.prompt("Resy auth token (the x-resy-auth-token value)")
        creds = {
            "api_key": resy_api_key,
            "auth_token": resy_auth_token,
            "payment_method_id": "",
        }

    resy_payment_id = creds.get("payment_method_id", "")
    if not resy_payment_id:
        click.echo("\nPayment method ID not found automatically.")
        click.echo("To find it: in the same Network tab, look for a request to")
        click.echo("api.resy.com/2/user — the response JSON has 'payment_methods' with an 'id'.")
        resy_payment_id = click.prompt(
            "Resy payment method ID (or press Enter to skip for now)",
            default="",
            show_default=False,
        )

    p = UserProfile(
        name=name,
        phone=phone,
        email=email,
        resy_api_key=creds.get("api_key", ""),
        resy_auth_token=creds.get("auth_token", ""),
        resy_email=resy_email,
        resy_password=resy_password,
        resy_payment_method_id=resy_payment_id or None,
    )
    path = save_profile(p, config_dir)
    click.echo(f"\nProfile saved to {path}")
    click.echo("Run 'python3 run.py profile show' to verify.")


@profile.command("login")
@click.pass_context
def profile_login(ctx):
    """Refresh Resy auth token by logging in again."""
    config_dir = ctx.obj["config_dir"]
    try:
        p = load_profile(config_dir)
    except FileNotFoundError as e:
        click.echo(str(e), err=True)
        sys.exit(1)

    resy_email = p.resy_email or click.prompt("Resy email")
    resy_password = p.resy_password or click.prompt("Resy password", hide_input=True)

    click.echo("Logging in to Resy...")
    try:
        from resbot.platforms.resy import ResyClient

        creds = asyncio.run(ResyClient.login(resy_email, resy_password))
    except Exception as e:
        click.echo(f"Login failed: {e}", err=True)
        sys.exit(1)

    p.resy_api_key = creds["api_key"]
    p.resy_auth_token = creds["auth_token"]
    p.resy_email = resy_email
    p.resy_password = resy_password
    if creds.get("payment_method_id"):
        p.resy_payment_method_id = creds["payment_method_id"]

    save_profile(p, config_dir)
    click.echo("Auth token refreshed successfully.")


@profile.command("show")
@click.pass_context
def profile_show(ctx):
    """Display current profile (secrets redacted)."""
    config_dir = ctx.obj["config_dir"]
    try:
        p = load_profile(config_dir)
    except FileNotFoundError as e:
        click.echo(str(e), err=True)
        sys.exit(1)

    click.echo(f"Name:        {p.name}")
    click.echo(f"Phone:       {p.phone}")
    click.echo(f"Email:       {p.email}")
    click.echo(f"Resy Key:    {_redact(p.resy_api_key)}")
    click.echo(f"Resy Token:  {_redact(p.resy_auth_token)}")
    click.echo(f"Payment ID:  {p.resy_payment_method_id or '(auto-detect)'}")


# ── Target commands ──


@cli.group()
def target():
    """Manage reservation targets."""


@target.command("add")
@click.option("--from-file", type=click.Path(exists=True, path_type=Path), help="Load from YAML file")
@click.pass_context
def target_add(ctx, from_file):
    """Add a new reservation target."""
    config_dir = ctx.obj["config_dir"]

    if from_file:
        with open(from_file) as f:
            data = yaml.safe_load(f)
        t = ReservationTarget(**data)
        path = save_target(t, config_dir)
        click.echo(f"Target '{t.id}' added from {from_file} → {path}")
        return

    click.echo("=== Add Reservation Target ===\n")

    target_id = click.prompt("Target ID (e.g. dorsia-dinner)")
    platform = click.prompt("Platform", type=click.Choice(["resy", "opentable"]), default="resy")
    venue_name = click.prompt("Restaurant name")
    venue_id = click.prompt("Venue ID (use 'resbot venue search' to find this)")
    party_size = click.prompt("Party size", type=int, default=2)
    meal_type = click.prompt(
        "Meal type",
        type=click.Choice([m.value for m in MealTime]),
        default="dinner",
    )

    meal = MealTime(meal_type)
    default_window = meal.default_window
    click.echo(f"\nDefault time window for {meal_type}: {default_window.earliest} - {default_window.latest}")
    customize_window = click.confirm("Customize time window?", default=False)

    time_window = None
    if customize_window:
        earliest = click.prompt("Earliest time (HH:MM)", default=default_window.earliest.strftime("%H:%M"))
        latest = click.prompt("Latest time (HH:MM)", default=default_window.latest.strftime("%H:%M"))
        time_window = TimeWindow(
            earliest=time.fromisoformat(earliest),
            latest=time.fromisoformat(latest),
        )

    preferred_str = click.prompt(
        "Preferred times (comma-separated HH:MM, or blank for default)",
        default="",
        show_default=False,
    )
    preferred_times = []
    if preferred_str:
        preferred_times = [time.fromisoformat(t.strip()) for t in preferred_str.split(",")]

    seating = click.prompt("Preferred seating (e.g. 'Dining Room', blank for any)", default="", show_default=False)

    start_date_str = click.prompt(
        "Start date for booking attempts (YYYY-MM-DD, blank for auto)",
        default="",
        show_default=False,
    )
    end_date_str = click.prompt(
        "End date for booking attempts (YYYY-MM-DD, blank for no limit)",
        default="",
        show_default=False,
    )
    start_date_val = date.fromisoformat(start_date_str) if start_date_str else None
    end_date_val = date.fromisoformat(end_date_str) if end_date_str else None

    days_ahead = click.prompt("Days in advance reservations open", type=int, default=14)
    drop_str = click.prompt("Drop time (HH:MM:SS)", default="00:00:00")
    drop_tz = click.prompt("Drop timezone", default="America/New_York")
    max_retries = click.prompt("Max retry days", type=int, default=30)
    snipe_rate = click.prompt("Snipe request rate (requests/sec, 1-50)", type=float, default=10.0)
    snipe_timeout = click.prompt("Snipe timeout in seconds (how long to keep trying after drop)", type=int, default=300)

    t = ReservationTarget(
        id=target_id,
        platform=platform,
        venue_id=venue_id,
        venue_name=venue_name,
        party_size=party_size,
        meal_type=meal,
        time_window=time_window,
        preferred_times=preferred_times,
        preferred_seating=seating or None,
        start_date=start_date_val,
        end_date=end_date_val,
        days_in_advance=days_ahead,
        drop_time=time.fromisoformat(drop_str),
        drop_timezone=drop_tz,
        max_retry_days=max_retries,
        snipe_rate=snipe_rate,
        snipe_timeout=snipe_timeout,
    )
    path = save_target(t, config_dir)
    click.echo(f"\nTarget '{t.id}' saved to {path}")


@target.command("list")
@click.pass_context
def target_list(ctx):
    """List all configured targets."""
    config_dir = ctx.obj["config_dir"]
    targets = load_targets(config_dir)
    if not targets:
        click.echo("No targets configured. Use 'resbot target add' to create one.")
        return
    for t in targets:
        status = "enabled" if t.enabled else "disabled"
        window = t.effective_window
        click.echo(
            f"  [{status}] {t.id}: {t.venue_name} | {t.platform} | "
            f"{t.meal_type.value} {window.earliest.strftime('%H:%M')}-{window.latest.strftime('%H:%M')} | "
            f"party of {t.party_size} | drop {t.drop_time.strftime('%H:%M:%S')} {t.drop_timezone}"
        )


@target.command("remove")
@click.argument("target_id")
@click.pass_context
def target_remove(ctx, target_id):
    """Remove a reservation target."""
    config_dir = ctx.obj["config_dir"]
    if remove_target(target_id, config_dir):
        click.echo(f"Target '{target_id}' removed.")
    else:
        click.echo(f"Target '{target_id}' not found.", err=True)
        sys.exit(1)


# ── Venue search ──


@cli.command("venue")
@click.argument("query")
@click.pass_context
def venue_search(ctx, query):
    """Search for a restaurant to get its venue ID."""
    config_dir = ctx.obj["config_dir"]

    async def _search():
        p = load_profile(config_dir)
        from resbot.platforms.resy import ResyClient

        client = ResyClient(p)
        try:
            results = await client.search_venues(query)
            if not results:
                click.echo("No results found via API.")
                _show_manual_venue_instructions()
                return
            click.echo(f"\nFound {len(results)} result(s):\n")
            for r in results:
                click.echo(f"  ID: {r['venue_id']}")
                click.echo(f"  Name: {r['name']}")
                click.echo(f"  Location: {r['location']}")
                click.echo(f"  Cuisine: {', '.join(r['cuisine']) if r['cuisine'] else 'N/A'}")
                click.echo()
        except Exception:
            click.echo("Venue search API is unavailable.")
            _show_manual_venue_instructions()
        finally:
            await client.close()

    asyncio.run(_search())


def _show_manual_venue_instructions():
    """Show instructions for finding venue ID manually."""
    click.echo("\nYou can find the venue ID manually from the Resy website:\n")
    click.echo("  1. Go to resy.com and search for the restaurant")
    click.echo("  2. Click on the restaurant page")
    click.echo("  3. Open Dev Tools (F12) > Network tab")
    click.echo("  4. Look for a request to api.resy.com/4/find")
    click.echo("  5. In the request payload/params, you'll see 'venue_id=XXXXX'")
    click.echo("     That number is the venue ID.\n")
    click.echo("  OR: Look at the URL — some pages show it as:")
    click.echo("     resy.com/cities/ny/venue-name?venue_id=XXXXX\n")


# ── Snipe commands ──


@cli.command("snipe")
@click.argument("target_id", required=False)
@click.option("--all", "snipe_all", is_flag=True, help="Snipe all enabled targets")
@click.option("--date", "snipe_date", default=None, help="Override target date (YYYY-MM-DD)")
@click.pass_context
def snipe(ctx, target_id, snipe_all, snipe_date):
    """Run an immediate snipe attempt."""
    config_dir = ctx.obj["config_dir"]

    if not target_id and not snipe_all:
        click.echo("Specify a target ID or use --all", err=True)
        sys.exit(1)

    override_date = date.fromisoformat(snipe_date) if snipe_date else None

    if snipe_all:
        from resbot.runner import run_all_snipes

        results = asyncio.run(run_all_snipes(config_dir, override_date=override_date))
        for r in results:
            icon = "OK" if r.success else "FAIL"
            click.echo(f"  [{icon}] {r.target_id}: {r.error or r.confirmation_token or 'booked'}")
    else:
        from resbot.runner import run_single_snipe

        result = asyncio.run(run_single_snipe(target_id, config_dir, override_date=override_date))
        if result.success:
            click.echo("")
            click.echo("=" * 55)
            click.echo("  *** RESERVATION CONFIRMED ***")
            click.echo(f"  Confirmation: {result.confirmation_token}")
            click.echo("=" * 55)
        else:
            click.echo(f"\nFailed: {result.error}", err=True)
            sys.exit(1)


# ── Grab (immediate book for already-open slots) ──


@cli.command("grab")
@click.argument("target_id")
@click.option("--date", "grab_date", required=True, help="Reservation date (YYYY-MM-DD)")
@click.pass_context
def grab(ctx, target_id, grab_date):
    """Immediately find and book an already-open slot. No waiting for drop time."""
    config_dir = ctx.obj["config_dir"]
    from resbot.config import load_target

    target = load_target(target_id, config_dir)
    search_date = date.fromisoformat(grab_date)

    if search_date < date.today():
        click.echo(f"Error: date {search_date} is in the past. Use a future date.", err=True)
        sys.exit(1)

    async def _grab():
        from resbot.engine import rank_slots
        from resbot.platforms.resy import ResyClient

        profile = load_profile(config_dir)
        client = ResyClient(profile)
        try:
            click.echo(f"Looking for slots at {target.venue_name} on {search_date} (party of {target.party_size})...")
            await client.warmup()
            slots, raw_data = await client.find_slots(target.venue_id, search_date, target.party_size)
            click.echo(f"Found {len(slots)} raw slot(s)")

            if not slots:
                import orjson
                raw_str = orjson.dumps(raw_data).decode()
                click.echo(f"No slots found. Raw API response ({len(raw_str)} bytes):")
                click.echo(raw_str[:2000])
                sys.exit(1)

            for s in slots:
                click.echo(f"  {s.slot_time.strftime('%H:%M')}  {s.table_type!r}")

            ranked = rank_slots(slots, target)
            click.echo(f"\n{len(ranked)} slot(s) after filtering. Attempting to book top match...")

            for slot in ranked[:3]:
                click.echo(f"  Trying {slot.slot_time.strftime('%H:%M')} ({slot.table_type})...")
                try:
                    token = await client.get_booking_token(slot, search_date, target.party_size)
                    result = await client.book(token)
                    if result.success:
                        click.echo("")
                        click.echo("=" * 55)
                        click.echo("  *** RESERVATION CONFIRMED ***")
                        click.echo(f"  Restaurant: {target.venue_name}")
                        click.echo(f"  Time:       {slot.slot_time.strftime('%H:%M')}")
                        click.echo(f"  Date:       {search_date}")
                        click.echo(f"  Party:      {target.party_size}")
                        click.echo(f"  Confirm:    {result.confirmation_token}")
                        click.echo("=" * 55)
                        return
                except Exception as e:
                    click.echo(f"  Failed: {e}")

            click.echo("\nCould not book any slot.", err=True)
            sys.exit(1)
        finally:
            await client.close()

    asyncio.run(_grab())


# ── Test find (dry run) ──


@cli.command("test-find")
@click.argument("target_id")
@click.option("--date", "find_date", required=True, help="Date to search (YYYY-MM-DD)")
@click.pass_context
def test_find(ctx, target_id, find_date):
    """Dry run: find available slots without booking. Shows what the API returns."""
    config_dir = ctx.obj["config_dir"]
    from resbot.config import load_target

    target = load_target(target_id, config_dir)
    search_date = date.fromisoformat(find_date)

    if search_date < date.today():
        click.echo(f"Error: date {search_date} is in the past. Use a future date.", err=True)
        sys.exit(1)

    async def _find():
        profile = load_profile(config_dir)
        from resbot.platforms.resy import ResyClient

        client = ResyClient(profile)
        try:
            click.echo(f"Searching {target.venue_name} (ID: {target.venue_id}) for {search_date} party of {target.party_size}...")
            click.echo(f"Using API key: {profile.resy_api_key[:8]}... Auth token: {profile.resy_auth_token[:8]}...")
            slots, raw_data = await client.find_slots(target.venue_id, search_date, target.party_size)

            # Always show raw API structure
            results_keys = list(raw_data.get("results", {}).keys())
            venues = raw_data.get("results", {}).get("venues", [])
            click.echo(f"\nAPI response structure:")
            click.echo(f"  Top-level keys: {list(raw_data.keys())}")
            click.echo(f"  results keys: {results_keys}")
            click.echo(f"  venues count: {len(venues)}")
            if venues:
                v = venues[0]
                click.echo(f"  First venue keys: {list(v.keys())}")
                click.echo(f"  First venue slot count: {len(v.get('slots', []))}")

            click.echo(f"\nParsed {len(slots)} slot(s):\n")
            for s in slots:
                click.echo(f"  {s.slot_time.strftime('%H:%M')}  type={s.table_type!r}  shift={s.shift_label!r}  token={s.config_token[:30]}...")

            if slots:
                from resbot.engine import rank_slots
                ranked = rank_slots(slots, target)
                click.echo(f"\nAfter filtering/ranking: {len(ranked)} slot(s)")
                for s in ranked[:10]:
                    click.echo(f"  {s.slot_time.strftime('%H:%M')}  type={s.table_type!r}")
            else:
                import orjson
                raw_str = orjson.dumps(raw_data).decode()
                click.echo(f"\nNo slots parsed. Raw API response ({len(raw_str)} bytes):")
                click.echo(raw_str[:2000])
        finally:
            await client.close()

    asyncio.run(_find())


# ── Run scheduler ──


@cli.command("run")
@click.pass_context
def run(ctx):
    """Start the reservation scheduler daemon."""
    config_dir = ctx.obj["config_dir"]
    from resbot.runner import run_scheduler

    click.echo("Starting resbot scheduler... (Ctrl+C to stop)")
    asyncio.run(run_scheduler(config_dir))


# ── Web dashboard ──


@cli.command("web")
@click.option("--port", type=int, default=8000, help="Dashboard port")
@click.pass_context
def web(ctx, port):
    """Launch the web status dashboard."""
    config_dir = ctx.obj["config_dir"]
    import uvicorn

    from resbot.web.app import create_app

    app = create_app(config_dir)
    click.echo(f"Starting dashboard at http://localhost:{port}")
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")


def _redact(s: str, show: int = 6) -> str:
    if not s:
        return "(not set)"
    if len(s) <= show:
        return "***"
    return s[:show] + "***"
