# resbot — Restaurant Reservation Bot

Speed-optimized bot that snipes restaurant reservations on Resy and OpenTable the moment they open.

## Setup

**Step 1: Install dependencies**

```bash
pip3 install -r requirements.txt
```

If `pip3` doesn't work, try `python3 -m pip install -r requirements.txt`.

**Step 2: Run it**

```bash
python3 run.py --help
```

That's it. No other install step needed.

> **Alternative install** (optional — gives you the `resbot` command):
> ```bash
> pip3 install -e .
> resbot --help
> ```

## Quick Start

### 1. Set up your profile

```bash
python3 run.py profile setup
```

You'll need your Resy credentials. To find them:
1. Go to resy.com and log in
2. Open browser Dev Tools (F12) → Network tab
3. Click around the site and look for requests to `api.resy.com`
4. Copy the **API key** from the `Authorization` header and the **auth token** from `X-Resy-Auth-Token`

### 2. Find a restaurant

```bash
python3 run.py venue "Restaurant Name"
```

This gives you the **venue ID** you need for the next step.

### 3. Add a reservation target

```bash
python3 run.py target add
```

It walks you through: restaurant, party size, meal type, time preferences, and when reservations open.

### 4. Run the bot

**Snipe now** (one-shot attempt):
```bash
python3 run.py snipe my-target-id
```

**Run the scheduler** (retries daily at drop time until booked):
```bash
python3 run.py run
```

**Web dashboard** (monitor everything in your browser):
```bash
python3 run.py web
```
Then open http://localhost:8000

## How It Works

1. You tell the bot which restaurant, date, party size, and meal time you want
2. The bot knows when that restaurant releases reservations (the "drop time" — usually midnight, X days in advance)
3. At drop time, it fires rapid burst requests (~10/sec) to grab a slot the instant it appears
4. It tries your top 3 time preferences in parallel — first one that books wins
5. If it fails, it retries the next day automatically
6. HTTP/2 persistent connections + connection warmup 30s before drop = sub-500ms booking pipeline

## Commands

| Command | Description |
|---------|-------------|
| `python3 run.py profile setup` | Set up your name, phone, email, Resy credentials |
| `python3 run.py profile show` | View your profile (secrets redacted) |
| `python3 run.py venue "query"` | Search for a restaurant's venue ID |
| `python3 run.py target add` | Add a new reservation target |
| `python3 run.py target list` | List all targets |
| `python3 run.py target remove <id>` | Remove a target |
| `python3 run.py snipe <id>` | Snipe a specific target right now |
| `python3 run.py snipe --all` | Snipe all enabled targets now |
| `python3 run.py run` | Start the automated scheduler |
| `python3 run.py web` | Launch the web dashboard |

## Configuration

All config lives in `~/.resbot/`:
- `~/.resbot/profile.yaml` — your credentials
- `~/.resbot/targets/*.yaml` — one file per reservation target

See `config/example_target.yaml` for a target config template.
