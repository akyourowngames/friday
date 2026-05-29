"""Markdown control-surface loader for the cognition substrate.

Reads `cognition/COGNITION_CONFIG.md` (path from settings) into a dict of
sections, each a dict of typed values. Parsing follows the same `## Section`
and `- key: value` shape used by the maintenance and memory policy files.

No regex, no keyword routing. Values are typed by attempt: int, then float,
then bool, then raw string.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from config import settings


def _resolve_path(path: Path) -> Path:
    if not path.is_absolute():
        path = Path(__file__).resolve().parent.parent / path
    return path


def _config_path() -> Path:
    return _resolve_path(Path(settings.cognition_config_file))


def _coerce(value: str):
    text = str(value).strip()
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
            current = line[3:].strip().lower()
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
def load_cognition_config() -> dict:
    """Return all sections parsed from the cognition markdown.

    Cached for process lifetime. Returns an empty dict when the file is absent
    so callers can fall back to their own defaults.
    """
    path = _config_path()
    if not path.exists():
        return {}
    return _parse(path.read_text(encoding="utf-8"))


def section_values(section: str, defaults: dict) -> dict:
    """Merge a config section over caller-provided defaults.

    The defaults define the contract (keys and value types). File values
    override defaults only when present, keeping behavior backward compatible
    when the markdown omits a key.
    """
    loaded = load_cognition_config().get(str(section).strip().lower(), {})
    merged = dict(defaults)
    for key, value in loaded.items():
        if key in merged:
            merged[key] = value
    return merged
