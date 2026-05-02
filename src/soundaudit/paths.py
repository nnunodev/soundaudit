"""Cross-platform path utilities for config, data, and logs."""

from __future__ import annotations

import os
import sys
from pathlib import Path

_APP_NAME = "soundaudit"


def get_data_dir() -> Path:
    """Platform-specific data directory (database, logs)."""
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        return base / _APP_NAME
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / _APP_NAME
    # Linux / *nix – respect XDG_DATA_HOME
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return base / _APP_NAME


def get_config_dir() -> Path:
    """Platform-specific config directory."""
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
        return base / _APP_NAME
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / _APP_NAME
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / _APP_NAME


def get_default_db_path() -> Path:
    get_data_dir().mkdir(parents=True, exist_ok=True)
    return get_data_dir() / "scan.db"


def get_log_path() -> Path:
    get_data_dir().mkdir(parents=True, exist_ok=True)
    return get_data_dir() / "soundaudit.log"


def find_config_file() -> Path | None:
    """Search standard locations for config.yaml / config.yml."""
    candidates = [
        Path("config.yaml"),
        Path("config.yml"),
        get_config_dir() / "config.yaml",
        get_config_dir() / "config.yml",
        Path.home() / f".{_APP_NAME}.yaml",
        Path.home() / f".{_APP_NAME}.yml",
    ]
    for c in candidates:
        if c.exists():
            return c
    return None
