from __future__ import annotations

from pathlib import Path

from .core import JsonObject


def str_arg(args: JsonObject, key: str, default: str = "") -> str:
    value = args.get(key, default)
    if isinstance(value, dict):
        for candidate in _field_candidates(key):
            nested = value.get(candidate)
            if nested is None or isinstance(nested, (dict, list)):
                continue
            return str(nested).strip()
        return ""
    if isinstance(value, list):
        return ""
    return str(value if value is not None else "").strip()


def _field_candidates(key: str) -> tuple[str, ...]:
    hints = {
        "project": ("name", "project_name", "id", "project_id"),
        "project_id": ("id", "project_id", "name"),
        "name": ("name", "title", "id"),
        "title": ("title", "name", "text", "value", "id"),
        "task": ("title", "task", "name", "id", "task_id"),
        "task_id": ("id", "task_id", "title"),
        "parent_task": ("title", "task", "name", "id", "task_id"),
        "parent_task_id": ("id", "task_id", "title"),
        "note": ("note", "text", "value"),
    }
    return hints.get(key, (key, "value", "text", "name", "title", "id"))


def int_arg(args: JsonObject, key: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(args.get(key, default))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def float_arg(args: JsonObject, key: str) -> float:
    try:
        return float(args[key])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"{key} must be a number") from exc


def safe_path(root: Path, raw_path: str) -> Path:
    raw = str(raw_path or ".").strip() or "."
    target = Path(raw)
    if not target.is_absolute():
        target = root / target
    resolved_root = root.resolve()
    resolved_target = target.resolve()
    try:
        resolved_target.relative_to(resolved_root)
    except ValueError as exc:
        raise ValueError("Path must stay inside the current workspace.") from exc
    return resolved_target


def relative_path(root: Path, path: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except ValueError:
        return str(path)
