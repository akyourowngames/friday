from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from config import settings


@dataclass
class StepConfig:
    name: str
    enabled: bool = True
    options: dict = field(default_factory=dict)


@dataclass
class MaintenanceConfig:
    repo_root: Path
    config_path: Path
    cutoff_time: str = "03:30"
    timezone: str = "local"
    min_run_interval_minutes: int = 60
    log_max_runs: int = 90
    enabled: bool = True
    steps: list[StepConfig] = field(default_factory=list)
    action_whitelist: set[str] = field(default_factory=set)
    retention: dict = field(default_factory=dict)

    def step(self, name: str) -> StepConfig | None:
        for step in self.steps:
            if step.name == name:
                return step
        return None

    def public_dict(self) -> dict:
        return {
            "config_path": str(self.config_path),
            "cutoff_time": self.cutoff_time,
            "timezone": self.timezone,
            "min_run_interval_minutes": self.min_run_interval_minutes,
            "log_max_runs": self.log_max_runs,
            "enabled": self.enabled,
            "steps": [
                {"name": step.name, "enabled": step.enabled, "options": dict(step.options)}
                for step in self.steps
            ],
            "action_whitelist": sorted(self.action_whitelist),
            "retention": dict(self.retention),
        }


def load_config(repo_root: str | Path = ".", config_path: str | Path | None = None) -> MaintenanceConfig:
    root = Path(repo_root).expanduser().resolve()
    if config_path is None:
        path = root / settings.maintenance_config_file
    else:
        path = Path(config_path).expanduser()
        if not path.is_absolute():
            path = root / path
    path = path.resolve()

    runtime_values: dict[str, str] = {}
    steps: list[StepConfig] = []
    action_whitelist: set[str] = set()
    retention: dict = {}

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
                    runtime_values[key.strip().lower()] = value.strip()
            elif section == "steps":
                step = _parse_step(item)
                if step is not None:
                    steps.append(step)
            elif section == "action_whitelist":
                if item:
                    action_whitelist.add(item)
            elif section == "retention":
                key, _, value = item.partition(":")
                if value:
                    retention[key.strip().lower()] = _parse_int(value.strip(), 0)

    return MaintenanceConfig(
        repo_root=root,
        config_path=path,
        cutoff_time=runtime_values.get("cutoff_time", "03:30"),
        timezone=runtime_values.get("timezone", "local"),
        min_run_interval_minutes=_parse_int(runtime_values.get("min_run_interval_minutes"), 60),
        log_max_runs=_parse_int(runtime_values.get("log_max_runs"), settings.maintenance_log_max_runs),
        enabled=_parse_bool(runtime_values.get("enabled"), True),
        steps=steps,
        action_whitelist=action_whitelist,
        retention=retention,
    )


def _parse_step(item: str) -> StepConfig | None:
    name, _, raw_options = item.partition(":")
    name = name.strip()
    if not name:
        return None
    options: dict = {}
    enabled = True
    for token in raw_options.split():
        token = token.strip()
        if not token or "=" not in token:
            continue
        key, _, value = token.partition("=")
        key = key.strip().lower()
        value = value.strip()
        if key == "enabled":
            enabled = _parse_bool(value, True)
        else:
            parsed_int = _maybe_int(value)
            if parsed_int is not None:
                options[key] = parsed_int
                continue
            parsed_bool = _maybe_bool(value)
            if parsed_bool is not None:
                options[key] = parsed_bool
                continue
            options[key] = value
    return StepConfig(name=name, enabled=enabled, options=options)


def _parse_bool(value, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off"):
        return False
    return default


def _parse_int(value, default: int) -> int:
    if value is None or isinstance(value, bool):
        return default
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _maybe_int(value: str) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _maybe_bool(value: str) -> bool | None:
    text = str(value or "").strip().lower()
    if text in ("true", "yes", "on"):
        return True
    if text in ("false", "no", "off"):
        return False
    return None
