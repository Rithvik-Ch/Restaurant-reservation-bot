"""Persistent activity log for resbot attempts.

Stores each grab/snipe/run result as a JSON-lines file at ~/.resbot/logs/.
One file per day: activity-YYYY-MM-DD.jsonl
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

from resbot.config import DEFAULT_CONFIG_DIR

logger = logging.getLogger(__name__)

LOGS_DIR_NAME = "logs"


def _logs_dir(config_dir: Path | None = None) -> Path:
    d = (config_dir or DEFAULT_CONFIG_DIR) / LOGS_DIR_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def log_attempt(
    *,
    target_id: str,
    action: str,
    target_date: str,
    success: bool,
    detail: str,
    venue_name: str = "",
    confirmation: str | None = None,
    config_dir: Path | None = None,
) -> None:
    """Append a single attempt record to today's log file."""
    now = datetime.now()
    record = {
        "timestamp": now.isoformat(timespec="seconds"),
        "target_id": target_id,
        "venue_name": venue_name,
        "action": action,
        "target_date": target_date,
        "success": success,
        "detail": detail,
    }
    if confirmation:
        record["confirmation"] = confirmation

    log_file = _logs_dir(config_dir) / f"activity-{now.strftime('%Y-%m-%d')}.jsonl"
    try:
        with open(log_file, "a") as f:
            f.write(json.dumps(record) + "\n")
    except Exception as e:
        logger.warning("Failed to write activity log: %s", e)


def read_logs(days: int = 7, config_dir: Path | None = None) -> list[dict]:
    """Read recent log entries, newest first. Returns up to `days` days of logs."""
    logs_dir = _logs_dir(config_dir)
    entries = []
    log_files = sorted(logs_dir.glob("activity-*.jsonl"), reverse=True)
    for log_file in log_files[:days]:
        try:
            with open(log_file) as f:
                for line in f:
                    line = line.strip()
                    if line:
                        entries.append(json.loads(line))
        except Exception as e:
            logger.warning("Failed to read log %s: %s", log_file.name, e)
    # Sort newest first
    entries.sort(key=lambda x: x.get("timestamp", ""), reverse=True)
    return entries
