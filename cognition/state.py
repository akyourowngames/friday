"""Persistence for the cognition substrate.

Stores cadence model, episodes, and proactive engine state as a single JSON file
written atomically. No regex, no hardcoded content.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from config import settings


def _state_path(path: str | Path | None = None) -> Path:
    target = Path(path or settings.cognition_state_path)
    if not target.is_absolute():
        target = Path(__file__).resolve().parent.parent / target
    return target


def load_state(path: str | Path | None = None) -> dict:
    target = _state_path(path)
    if not target.exists():
        return {}
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_state(payload: dict, path: str | Path | None = None) -> str:
    target = _state_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False)
        os.replace(temp, target)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)
    return str(target)
