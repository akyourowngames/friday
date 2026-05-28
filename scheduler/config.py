from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from config import settings


@dataclass
class SchedulerConfig:
    repo_root: Path
    config_path: Path
    check_interval_seconds: int = 30
    max_items_per_run: int = 50
    default_timezone: str = "local"
    action_whitelist: set[str] = field(default_factory=set)
    memory_linkage: dict = field(default_factory=dict)
    notes_linkage: dict = field(default_factory=dict)
    folder_linkage: dict = field(default_factory=dict)
    reschedule_policy: dict = field(default_factory=dict)

    def public_dict(self) -> dict:
        return {
            "config_path": str(self.config_path),
            "check_interval_seconds": self.check_interval_seconds,
            "max_items_per_run": self.max_items_per_run,
            "default_timezone": self.default_timezone,
            "action_whitelist": sorted(self.action_whitelist),
            "memory_linkage": dict(self.memory_linkage),
            "notes_linkage": dict(self.notes_linkage),
            "folder_linkage": dict(self.folder_linkage),
            "reschedule_policy": dict(self.reschedule_policy),
        }


def load_config(repo_root: str | Path = ".", config_path: str | Path | None = None) -> SchedulerConfig:
    root = Path(repo_root).expanduser().resolve()
    if config_path is None:
        path = root / settings.scheduler_config_file
    else:
        path = Path(config_path).expanduser()
        if not path.is_absolute():
            path = root / path
    path = path.resolve()

    runtime: dict[str, str] = {}
    whitelist: set[str] = set()
    memory_linkage: dict = {}
    notes_linkage: dict = {}
    folder_linkage: dict = {}
    reschedule: dict = {}

    if path.exists():
        section = ""
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line:
                continue
            if line.startswith("## "):
                section = line[3:].strip().lower().replace(" ", "_")
                continue
            if not line.startswith("- "):
                continue
            item = line[2:].strip()
            if section == "runtime":
                key, _, value = item.partition(":")
                if value:
                    runtime[key.strip().lower()] = value.strip()
            elif section == "action_whitelist":
                if item:
                    whitelist.add(item)
            elif section == "memory_linkage":
                _ingest_kv(item, memory_linkage)
            elif section == "notes_linkage":
                _ingest_kv(item, notes_linkage)
            elif section == "folder_watcher_linkage":
                _ingest_kv(item, folder_linkage)
            elif section == "reschedule_policy":
                _ingest_kv(item, reschedule)

    return SchedulerConfig(
        repo_root=root,
        config_path=path,
        check_interval_seconds=_parse_int(runtime.get("check_interval_seconds"), settings.scheduler_check_interval_seconds),
        max_items_per_run=_parse_int(runtime.get("max_items_per_run"), 50),
        default_timezone=runtime.get("default_timezone", "local"),
        action_whitelist=whitelist,
        memory_linkage=memory_linkage,
        notes_linkage=notes_linkage,
        folder_linkage=folder_linkage,
        reschedule_policy=reschedule,
    )


def _ingest_kv(item: str, target: dict) -> None:
    key, _, value = item.partition(":")
    key = key.strip().lower()
    value = value.strip().strip('"').strip("'")
    if not key or not value:
        return
    target[key] = _coerce(value)


def _coerce(value: str):
    text = str(value or "").strip()
    if not text:
        return text
    lower = text.lower()
    if lower in ("true", "yes", "on"):
        return True
    if lower in ("false", "no", "off"):
        return False
    try:
        if "." in text:
            return float(text)
        return int(text)
    except ValueError:
        return text


def _parse_int(value, default: int) -> int:
    if value is None or isinstance(value, bool):
        return default
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default
