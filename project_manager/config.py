"""Markdown control-surface loader for the project manager.

Reads `tools/PROJECT_MANAGER_CONFIG.md` (path from settings) into typed sections,
following the same `## Section` / `- key: value` shape used by the maintenance,
scheduler, and cognition control surfaces. Defaults define the contract; file
values override only when present, so behavior stays backward compatible when
the markdown omits a key.

No regex, no keyword routing. Values are typed by attempt: int, float, bool,
then raw string.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from config import settings

_DEFAULTS: dict[str, dict] = {
    "runtime": {
        "history_keep_points": 60,
        "updates_keep": 200,
        "alerts_keep": 40,
        "archive_keep": 50,
    },
    "scoring": {
        "momentum_window_days": 7,
        "velocity_window_days": 14,
        "health_blocker_penalty": 12,
        "health_overdue_penalty": 25,
        "health_stall_penalty": 20,
        "health_sentiment_penalty": 18,
        "health_scope_penalty": 10,
        "momentum_close_weight": 0.6,
        "momentum_update_weight": 0.4,
        "momentum_expected_per_week": 4,
    },
    "triggers": {
        "inactivity_enabled": True,
        "inactivity_days": 4,
        "velocity_collapse_enabled": True,
        "velocity_collapse_ratio": 0.35,
        "deadline_proximity_enabled": True,
        "deadline_warn_days": 10,
        "blocker_age_enabled": True,
        "blocker_age_days": 3,
        "health_drop_enabled": True,
        "health_drop_points": 15,
        "health_drop_window_hours": 48,
        "scope_expansion_enabled": True,
        "scope_growth_ratio": 1.25,
        "cross_project_conflict_enabled": True,
        "conflict_window_days": 3,
        "sentiment_deterioration_enabled": True,
        "sentiment_streak": 3,
        "ghost_detection_enabled": True,
        "ghost_days": 7,
        "ghost_max_updates": 1,
    },
    "status_thresholds": {
        "stalling_health_below": 60,
        "ghost_health_below": 35,
    },
    "intake": {
        "infer_tasks_min": 3,
        "infer_tasks_max": 5,
        "intake_max_tokens": 700,
        "intake_retries": 1,
    },
    "brief": {
        "worry_health_below": 55,
        "focus_top_n": 3,
        "push_desktop_notification": True,
    },
    "integrations": {
        "github_correlation_enabled": False,
        "gmail_correlation_enabled": False,
    },
    "obsidian_export": {
        "enabled": True,
        "subfolder": "Projects",
        "include_archived": True,
        "context_brief": True,
    },
}


def _resolve_path(path: Path) -> Path:
    if not path.is_absolute():
        path = Path(__file__).resolve().parent.parent / path
    return path


def _config_path() -> Path:
    return _resolve_path(Path(settings.project_manager_config_file))


def _coerce(value: str):
    text = str(value).strip().strip('"').strip("'")
    if not text:
        return text
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        pass
    lowered = text.lower()
    if lowered in ("true", "yes", "on"):
        return True
    if lowered in ("false", "no", "off"):
        return False
    return text


def _parse(text: str) -> dict:
    sections: dict[str, dict] = {}
    current = ""
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line.startswith("## "):
            current = line[3:].strip().lower().replace(" ", "_")
            sections.setdefault(current, {})
            continue
        if not current or not line.startswith("- ") or ":" not in line:
            continue
        key, _, value = line[2:].partition(":")
        key = key.strip().lower()
        if not key:
            continue
        sections[current][key] = _coerce(value)
    return sections


@lru_cache(maxsize=1)
def _load_file() -> dict:
    path = _config_path()
    if not path.exists():
        return {}
    return _parse(path.read_text(encoding="utf-8"))


def section(name: str) -> dict:
    """Merge a config section over its built-in defaults.

    The defaults define the keys and value types; file values override only when
    present. Unknown file keys are ignored so the contract stays stable.
    """
    key = str(name).strip().lower()
    defaults = dict(_DEFAULTS.get(key, {}))
    loaded = _load_file().get(key, {})
    for k, v in loaded.items():
        if k in defaults:
            defaults[k] = v
    return defaults


def value(section_name: str, key: str):
    return section(section_name).get(key)
