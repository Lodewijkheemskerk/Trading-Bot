"""
Configuration loader for the Prediction Market Trading Bot.

Loads settings from settings.yaml and exposes project path constants.
All data directories are auto-created on import.
"""

import os
import yaml
from pathlib import Path
from typing import Any, Optional


def load_settings() -> dict:
    """Load the full settings dict from settings.yaml."""
    config_path = Path(__file__).parent / "settings.yaml"
    with open(config_path, "r") as f:
        return yaml.safe_load(f)


def get_setting(key_path: str, default: Any = None) -> Any:
    """
    Get a nested setting by dot-separated path.

    Example:
        get_setting("risk.kelly_fraction")  -> 0.25
        get_setting("scanner.min_volume")   -> 50
    """
    settings = load_settings()
    value = settings
    for key in key_path.split("."):
        if isinstance(value, dict) and key in value:
            value = value[key]
        else:
            return default
    return value


# ---------------------------------------------------------------------------
# Project path constants
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).parent.parent
DATA_DIR = PROJECT_ROOT / "data"
TRADES_DIR = DATA_DIR / "trades"
MARKET_DIR = DATA_DIR / "market_snapshots"
RESEARCH_DIR = DATA_DIR / "research_briefs"
LOGS_DIR = PROJECT_ROOT / "logs"
REFERENCES_DIR = PROJECT_ROOT / "references"
KILL_SWITCH_FILE = PROJECT_ROOT / get_setting("execution.kill_switch_file", "STOP")

# Auto-create data directories on import
for _dir in (DATA_DIR, TRADES_DIR, MARKET_DIR, RESEARCH_DIR, LOGS_DIR):
    _dir.mkdir(parents=True, exist_ok=True)
