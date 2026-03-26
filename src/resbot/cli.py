"""CLI interface for resbot."""

from __future__ import annotations

import asyncio
import logging
import sys
from datetime import time
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
    days_ahead = click.prompt("Days in advance reservations open", type=int, default=14)
    drop_str = click.prompt("Drop time (HH:MM:SS)", default="00:00:00")
    drop_tz = click.prompt("Drop timezone", default="America/New_York")
    max_retries = click.prompt("Max retry days", type=int, default=30)

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
        days_in_advance=days_ahead,
        drop_time=time.fromisoformat(drop_str),
        drop_timezone=drop_tz,
        max_retry_days=max_retries,
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
                click.echo("No results found.")
                return
            click.echo(f"\nFound {len(results)} result(s):\n")
            for r in results:
                click.echo(f"  ID: {r['venue_id']}")
                click.echo(f"  Name: {r['name']}")
                click.echo(f"  Location: {r['location']}")
                click.echo(f"  Cuisine: {', '.join(r['cuisine']) if r['cuisine'] else 'N/A'}")
                click.echo()
        finally:
            await client.close()

    asyncio.run(_search())


# ── Snipe commands ──


@cli.command("snipe")
@click.argument("target_id", required=False)
@click.option("--all", "snipe_all", is_flag=True, help="Snipe all enabled targets")
@click.pass_context
def snipe(ctx, target_id, snipe_all):
    """Run an immediate snipe attempt."""
    config_dir = ctx.obj["config_dir"]

    if not target_id and not snipe_all:
        click.echo("Specify a target ID or use --all", err=True)
        sys.exit(1)

    if snipe_all:
        from resbot.runner import run_all_snipes

        results = asyncio.run(run_all_snipes(config_dir))
        for r in results:
            icon = "OK" if r.success else "FAIL"
            click.echo(f"  [{icon}] {r.target_id}: {r.error or r.confirmation_token or 'booked'}")
    else:
        from resbot.runner import run_single_snipe

        result = asyncio.run(run_single_snipe(target_id, config_dir))
        if result.success:
            click.echo(f"SUCCESS! Confirmation: {result.confirmation_token}")
        else:
            click.echo(f"Failed: {result.error}", err=True)
            sys.exit(1)


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
