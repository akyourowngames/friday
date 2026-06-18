"""Configuration management for Ares."""

import json
from pathlib import Path

from ares.models import AppConfig

DEFAULT_DATA_DIR = Path("~/.ares/data").expanduser()
CONFIG_PATH = Path("~/.ares/config.json").expanduser()


def ensure_data_dir(data_dir: Path | None = None) -> Path:
    """Create the data directory if it doesn't exist. Returns the path."""
    d = data_dir or Path(load_config().data_dir).expanduser()
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_db_path(data_dir: Path | None = None) -> Path:
    """Return the path to the SQLite database file."""
    return ensure_data_dir(data_dir) / "ares.db"


def load_config() -> AppConfig:
    """Load config from ~/.ares/config.json, or return defaults."""
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH) as f:
            data = json.load(f)
        return AppConfig(**data)
    return AppConfig()


def save_config(config: AppConfig) -> None:
    """Save config to ~/.ares/config.json."""
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_PATH, "w") as f:
        json.dump(config.model_dump(), f, indent=2)
