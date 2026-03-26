"""Configuration loading and management for resbot."""

from __future__ import annotations

import logging
from pathlib import Path

import yaml

from resbot.models import ReservationTarget, UserProfile

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_DIR = Path.home() / ".resbot"
PROFILE_FILE = "profile.yaml"
TARGETS_DIR = "targets"


def ensure_config_dir(config_dir: Path | None = None) -> Path:
    """Create config directory structure if it doesn't exist."""
    config_dir = config_dir or DEFAULT_CONFIG_DIR
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / TARGETS_DIR).mkdir(exist_ok=True)
    return config_dir


def load_profile(config_dir: Path | None = None) -> UserProfile:
    """Load user profile from YAML."""
    config_dir = config_dir or DEFAULT_CONFIG_DIR
    profile_path = config_dir / PROFILE_FILE
    if not profile_path.exists():
        raise FileNotFoundError(
            f"No profile found at {profile_path}. Run 'resbot profile setup' first."
        )
    with open(profile_path) as f:
        data = yaml.safe_load(f)
    return UserProfile(**data)


def save_profile(profile: UserProfile, config_dir: Path | None = None) -> Path:
    """Save user profile to YAML."""
    config_dir = ensure_config_dir(config_dir)
    profile_path = config_dir / PROFILE_FILE
    with open(profile_path, "w") as f:
        yaml.dump(profile.model_dump(exclude_none=True), f, default_flow_style=False)
    profile_path.chmod(0o600)
    return profile_path


def load_targets(config_dir: Path | None = None) -> list[ReservationTarget]:
    """Load all reservation targets from YAML files."""
    config_dir = config_dir or DEFAULT_CONFIG_DIR
    targets_dir = config_dir / TARGETS_DIR
    if not targets_dir.exists():
        return []
    targets = []
    for yaml_file in sorted(targets_dir.glob("*.yaml")):
        try:
            with open(yaml_file) as f:
                data = yaml.safe_load(f)
            if data:
                targets.append(ReservationTarget(**data))
        except Exception as e:
            logger.warning("Failed to load target %s: %s", yaml_file.name, e)
    return targets


def load_target(target_id: str, config_dir: Path | None = None) -> ReservationTarget:
    """Load a single reservation target by ID."""
    config_dir = config_dir or DEFAULT_CONFIG_DIR
    target_path = config_dir / TARGETS_DIR / f"{target_id}.yaml"
    if not target_path.exists():
        raise FileNotFoundError(f"Target '{target_id}' not found at {target_path}")
    with open(target_path) as f:
        data = yaml.safe_load(f)
    return ReservationTarget(**data)


def save_target(target: ReservationTarget, config_dir: Path | None = None) -> Path:
    """Save a reservation target to YAML."""
    config_dir = ensure_config_dir(config_dir)
    target_path = config_dir / TARGETS_DIR / f"{target.id}.yaml"
    data = target.model_dump(mode="json", exclude_none=True)
    with open(target_path, "w") as f:
        yaml.dump(data, f, default_flow_style=False)
    return target_path


def remove_target(target_id: str, config_dir: Path | None = None) -> bool:
    """Remove a reservation target YAML file."""
    config_dir = config_dir or DEFAULT_CONFIG_DIR
    target_path = config_dir / TARGETS_DIR / f"{target_id}.yaml"
    if target_path.exists():
        target_path.unlink()
        return True
    return False
