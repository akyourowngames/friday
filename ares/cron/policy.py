"""Validation and deterministic enforcement for advanced cron policies."""

from __future__ import annotations

import re
from datetime import datetime, time, timezone
from typing import Any
from zoneinfo import ZoneInfo


_DAY_NAMES = {
    "mon": 0, "monday": 0, "tue": 1, "tuesday": 1, "wed": 2, "wednesday": 2,
    "thu": 3, "thursday": 3, "fri": 4, "friday": 4, "sat": 5, "saturday": 5,
    "sun": 6, "sunday": 6,
}


def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    if not minimum <= number <= maximum:
        raise ValueError(f"Policy value must be between {minimum} and {maximum}")
    return number


def _clock(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", text):
        raise ValueError(f"{field} must use 24-hour HH:MM format")
    return text


def _window(value: Any, field: str) -> dict[str, Any]:
    if not value:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be an object")
    days = value.get("days", list(range(7)))
    if not isinstance(days, list):
        raise ValueError(f"{field}.days must be an array")
    normalized_days = []
    for item in days:
        if isinstance(item, int) and 0 <= item <= 6:
            normalized_days.append(item)
            continue
        day = _DAY_NAMES.get(str(item).strip().casefold())
        if day is None:
            raise ValueError(f"Unknown day in {field}: {item}")
        normalized_days.append(day)
    return {
        "start": _clock(value.get("start", "00:00"), f"{field}.start"),
        "end": _clock(value.get("end", "23:59"), f"{field}.end"),
        "days": sorted(set(normalized_days)),
    }


def normalize_cron_policy(value: Any) -> dict[str, Any]:
    """Normalize optional policy JSON while rejecting unbounded controls."""
    if value in (None, {}):
        return {}
    if not isinstance(value, dict):
        raise ValueError("Cron policy must be an object")
    allowed = {
        "window", "quiet_hours", "run_caps", "missed_runs", "retry", "dependencies",
        "concurrency_key", "notifications", "pause_after_failures", "variables", "budget",
        "expires_at",
    }
    unknown = set(value) - allowed
    if unknown:
        raise ValueError(f"Unknown cron policy field(s): {', '.join(sorted(unknown))}")
    policy: dict[str, Any] = {}
    if value.get("window"):
        policy["window"] = _window(value["window"], "policy.window")
    if value.get("quiet_hours"):
        policy["quiet_hours"] = _window(value["quiet_hours"], "policy.quiet_hours")
    caps = dict(value.get("run_caps") or {})
    if caps:
        policy["run_caps"] = {
            "max_total": _bounded_int(caps.get("max_total", 100_000), 100_000, 1, 1_000_000),
            "max_per_day": _bounded_int(caps.get("max_per_day", 1_000), 1_000, 1, 10_000),
        }
    missed = dict(value.get("missed_runs") or {})
    if missed:
        mode = str(missed.get("mode") or "run_once").casefold()
        if mode not in {"skip", "run_once", "catch_up"}:
            raise ValueError("policy.missed_runs.mode must be skip, run_once, or catch_up")
        policy["missed_runs"] = {
            "mode": mode,
            "grace_seconds": _bounded_int(missed.get("grace_seconds", 300), 300, 0, 31_536_000),
            "max_catch_up": _bounded_int(missed.get("max_catch_up", 1), 1, 1, 20),
        }
    retry = dict(value.get("retry") or {})
    if retry:
        multiplier = float(retry.get("multiplier", 2.0))
        if not 1.0 <= multiplier <= 10.0:
            raise ValueError("policy.retry.multiplier must be between 1 and 10")
        policy["retry"] = {
            "max_attempts": _bounded_int(retry.get("max_attempts", 1), 1, 1, 10),
            "base_seconds": _bounded_int(retry.get("base_seconds", 60), 60, 1, 86_400),
            "multiplier": multiplier,
            "max_seconds": _bounded_int(retry.get("max_seconds", 21_600), 21_600, 1, 604_800),
        }
    dependencies = value.get("dependencies") or []
    if not isinstance(dependencies, list) or len(dependencies) > 50:
        raise ValueError("policy.dependencies must be an array with at most 50 job IDs")
    if dependencies:
        policy["dependencies"] = list(dict.fromkeys(str(item).strip() for item in dependencies if str(item).strip()))
    concurrency_key = str(value.get("concurrency_key") or "").strip()
    if concurrency_key:
        if len(concurrency_key) > 100:
            raise ValueError("policy.concurrency_key is too long")
        policy["concurrency_key"] = concurrency_key
    notifications = value.get("notifications") or {}
    if notifications:
        if not isinstance(notifications, dict):
            raise ValueError("policy.notifications must be an object")
        statuses = notifications.get("on", ["completed", "failed"])
        if not isinstance(statuses, list) or any(item not in {"completed", "failed", "retrying"} for item in statuses):
            raise ValueError("policy.notifications.on contains an unsupported status")
        policy["notifications"] = {"on": list(dict.fromkeys(statuses))}
    if value.get("pause_after_failures") is not None:
        policy["pause_after_failures"] = _bounded_int(value["pause_after_failures"], 3, 1, 100)
    variables = value.get("variables") or {}
    if variables:
        if not isinstance(variables, dict) or len(variables) > 100:
            raise ValueError("policy.variables must be an object with at most 100 values")
        normalized_variables = {}
        for key, item in variables.items():
            name = str(key)
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", name):
                raise ValueError(f"Invalid cron variable name: {name}")
            text = str(item)
            if len(text) > 10_000:
                raise ValueError(f"Cron variable {name} is too large")
            normalized_variables[name] = text
        policy["variables"] = normalized_variables
    budget = value.get("budget") or {}
    if budget:
        if not isinstance(budget, dict):
            raise ValueError("policy.budget must be an object")
        policy["budget"] = {
            "max_iterations": _bounded_int(budget.get("max_iterations", 10), 10, 1, 100),
            "max_duration_seconds": _bounded_int(budget.get("max_duration_seconds", 900), 900, 5, 86_400),
            "max_output_chars": _bounded_int(budget.get("max_output_chars", 200_000), 200_000, 1_000, 2_000_000),
        }
    expires_at = str(value.get("expires_at") or "").strip()
    if expires_at:
        _parse_iso(expires_at)
        policy["expires_at"] = expires_at
    return policy


def _parse_iso(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _inside_window(now: datetime, window: dict[str, Any]) -> bool:
    if not window or now.weekday() not in set(window.get("days", range(7))):
        return not bool(window)
    start = time.fromisoformat(window["start"])
    end = time.fromisoformat(window["end"])
    current = now.timetz().replace(tzinfo=None)
    return start <= current <= end if start <= end else current >= start or current <= end


def cron_policy_block_reason(
    job: dict[str, Any], jobs: dict[str, dict[str, Any]], *, now: datetime | None = None,
) -> str:
    """Return one deterministic reason a run is blocked, or an empty string."""
    policy = normalize_cron_policy(job.get("policy") or {})
    now_utc = now or datetime.now(timezone.utc)
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    if policy.get("expires_at") and _parse_iso(policy["expires_at"]) <= now_utc:
        return "expired"
    caps = policy.get("run_caps") or {}
    if caps.get("max_total") and int(job.get("run_count") or 0) >= int(caps["max_total"]):
        return "maximum total runs reached"
    history = [_parse_iso(item) for item in job.get("run_history", [])]
    zone = ZoneInfo(str(job.get("timezone") or "UTC"))
    local_now = now_utc.astimezone(zone)
    if caps.get("max_per_day"):
        today = sum(item.astimezone(zone).date() == local_now.date() for item in history)
        if today >= int(caps["max_per_day"]):
            return "maximum daily runs reached"
    if policy.get("window") and not _inside_window(local_now, policy["window"]):
        return "outside execution window"
    if policy.get("quiet_hours") and _inside_window(local_now, policy["quiet_hours"]):
        return "inside quiet hours"
    for dependency_id in policy.get("dependencies", []):
        dependency = jobs.get(dependency_id)
        if dependency is None:
            return f"dependency not found: {dependency_id}"
        if dependency.get("last_status") != "completed":
            return f"dependency incomplete: {dependency_id}"
    key = policy.get("concurrency_key")
    if key:
        for other_id, other in jobs.items():
            if other_id != job.get("id") and other.get("state") == "running" and (other.get("policy") or {}).get("concurrency_key") == key:
                return f"concurrency group busy: {key}"
    return ""


def validate_dependency_graph(jobs: dict[str, dict[str, Any]]) -> None:
    """Reject missing/self/cyclic dependency graphs at the transaction boundary."""
    edges = {
        job_id: list(normalize_cron_policy(job.get("policy") or {}).get("dependencies", []))
        for job_id, job in jobs.items()
    }
    for job_id, dependencies in edges.items():
        missing = [item for item in dependencies if item not in jobs]
        if missing:
            raise ValueError(f"Cron job '{job_id}' references missing dependencies: {', '.join(missing)}")
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(job_id: str) -> None:
        if job_id in visiting:
            raise ValueError(f"Cron dependency cycle detected at '{job_id}'")
        if job_id in visited:
            return
        visiting.add(job_id)
        for dependency_id in edges[job_id]:
            visit(dependency_id)
        visiting.remove(job_id)
        visited.add(job_id)

    for job_id in edges:
        visit(job_id)


def expand_variables(prompt: str, policy: dict[str, Any]) -> str:
    values = normalize_cron_policy(policy).get("variables", {})
    return re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", lambda match: values.get(match.group(1), match.group(0)), prompt)


__all__ = [
    "cron_policy_block_reason", "expand_variables", "normalize_cron_policy",
    "validate_dependency_graph",
]
