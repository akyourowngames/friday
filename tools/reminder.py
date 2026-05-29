"""Reminder tool.

Fixes the false-negative where KING claimed it could not create reminders. It
schedules a notification through the existing scheduler engine, accepting
natural relative time ("in 5 min", "2 hours") or ISO datetimes. The fired
reminder records itself to memory and pushes a desktop notification when
available, degrading to a stored record otherwise.

Config-driven: the notify action is the registry tool `reminder_fire`, which the
scheduler whitelist in SCHEDULER_CONFIG.md authorizes.
"""

import json
import time
from datetime import datetime

from tools.registry import tool
from tools.runtime import (
    coerce_bool,
    emit_trace,
    error_payload,
    make_trace,
    normalize_response_format,
    structured_error,
    structured_success,
    utc_now_iso,
)
from tools.timeparse import resolve_when

_REMINDER_VERSION = "1.0.0"


def _trace(name, started_at, started, schema_valid, status, output_fields, error_code=None):
    return make_trace(
        name, _REMINDER_VERSION, started_at, started, 1, schema_valid,
        "reminder", status, output_fields, {"count": 1, "systems": ["scheduler_store"]}, error_code,
    )


def _emit(name, started, started_at, trace_enabled, result=None, error=None, response_format="legacy", legacy="", status="SUCCESS"):
    valid = error is None
    trace = _trace(name, started_at, started, valid, status if valid else "FAILED",
                   len(result) if result else 1, None if valid else error["code"])
    emit_trace(trace, trace_enabled)
    if response_format == "structured":
        if valid:
            return structured_success(name, _REMINDER_VERSION, result, started, trace)
        return structured_error(name, _REMINDER_VERSION, error, started, trace)
    return legacy


def _desktop_notify(title: str, message: str) -> bool:
    """Best-effort local desktop toast. Returns True only when proven sent."""
    try:
        import ctypes

        ctypes.windll.user32.MessageBeep(0xFFFFFFFF)
    except Exception:
        pass
    try:
        from winotify import Notification  # type: ignore

        Notification(app_id="KING", title=title, msg=message).show()
        return True
    except Exception:
        return False


@tool(
    name="reminder",
    description="Set a reminder for later using natural time like 'in 5 min', '2 hours', or an ISO datetime. KING schedules it and notifies you when due.",
    examples=[
        "remind me to do hw in 5 min",
        "set a reminder to call mom in 2 hours",
        "remind me to submit the form at 2026-05-30T09:00:00",
    ],
    param_descriptions={
        "task": "What to be reminded about",
        "when": "When to remind: relative ('in 5 min', '2 hours') or ISO datetime",
        "response_format": "legacy or structured",
        "trace_enabled": "When true, emit a machine-readable trace entry",
    },
)
def reminder(task: str, when: str, response_format: str = "legacy", trace_enabled: bool = False):
    started = time.perf_counter()
    started_at = utc_now_iso()
    response_format = normalize_response_format(response_format)
    trace_enabled = coerce_bool(trace_enabled)

    clean_task = str(task or "").strip()
    if not clean_task:
        err = error_payload("EMPTY_TASK", "task must not be empty.", "task", task, "non-empty reminder text", False, "Say what to be reminded about.")
        return _emit("reminder", started, started_at, trace_enabled, error=err, response_format=response_format, legacy="Error: reminder task is required", status="FAILED")

    target, mode = resolve_when(when, datetime.now())
    if target is None:
        err = error_payload("UNRESOLVED_TIME", "Could not resolve the reminder time.", "when", when, "relative like 'in 5 min' or ISO datetime", False, "Try 'in 10 minutes' or an ISO datetime.")
        return _emit("reminder", started, started_at, trace_enabled, error=err, response_format=response_format, legacy=f"Error: could not understand the time '{when}'", status="FAILED")

    from scheduler.engine import build_scheduler

    scheduler = build_scheduler(allowed_actions=_reminder_actions())
    try:
        record = scheduler.schedule(
            title=f"Reminder: {clean_task}",
            action="reminder_fire",
            scheduled_for=target.isoformat(timespec="seconds"),
            arguments={"task": clean_task},
            tags=["reminder"],
        )
    except ValueError as exc:
        err = error_payload("SCHEDULE_REJECTED", str(exc), "scheduler", None, "valid reminder", False, "Confirm reminder_fire is whitelisted in SCHEDULER_CONFIG.md.")
        return _emit("reminder", started, started_at, trace_enabled, error=err, response_format=response_format, legacy=f"Error: {exc}", status="FAILED")

    when_human = target.strftime("%I:%M %p on %b %d").lstrip("0")
    legacy = f"Reminder set: '{clean_task}' at {when_human} (id {record['id']})."
    result = {"item": record, "scheduled_for": record["scheduled_for"], "time_mode": mode, "task": clean_task}
    return _emit("reminder", started, started_at, trace_enabled, result=result, response_format=response_format, legacy=legacy)


@tool(
    name="reminder_fire",
    description="Internal action invoked by the scheduler when a reminder is due. Notifies the user and records the reminder. Not for direct manual use.",
    examples=["fire a due reminder"],
    param_descriptions={
        "task": "The reminder text to surface",
        "response_format": "legacy or structured",
        "trace_enabled": "When true, emit a machine-readable trace entry",
    },
)
def reminder_fire(task: str, response_format: str = "legacy", trace_enabled: bool = False):
    started = time.perf_counter()
    started_at = utc_now_iso()
    response_format = normalize_response_format(response_format)
    trace_enabled = coerce_bool(trace_enabled)
    clean_task = str(task or "").strip() or "(no detail)"
    notified = _desktop_notify("KING Reminder", clean_task)
    legacy = f"Reminder due: {clean_task}" + ("" if notified else " (no desktop notifier available)")
    result = {"task": clean_task, "desktop_notified": notified, "fired_at": started_at}
    return _emit("reminder_fire", started, started_at, trace_enabled, result=result, response_format=response_format, legacy=legacy)


def _reminder_actions():
    # reminder_fire must be runnable by the scheduler; merge with config whitelist.
    from scheduler.config import load_config

    cfg = load_config(".")
    return set(cfg.action_whitelist) | {"reminder_fire"}
