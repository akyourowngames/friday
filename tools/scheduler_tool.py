import json
import time

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

_SCHEDULER_VERSION = "1.0.0"


def _trace(name: str, started_at: str, started: float, schema_valid: bool, status: str, output_fields: int, error_code: str | None = None) -> dict:
    return make_trace(
        name,
        _SCHEDULER_VERSION,
        started_at,
        started,
        1,
        schema_valid,
        "scheduler_tool",
        status,
        output_fields,
        {"count": 1, "systems": ["scheduler_store"]},
        error_code,
    )


def _error(name: str, error: dict, response_format: str, trace_enabled: bool, started: float, started_at: str, legacy: str):
    trace = _trace(name, started_at, started, False, "FAILED", 1, error["code"])
    emit_trace(trace, trace_enabled)
    if response_format == "structured":
        return structured_error(name, _SCHEDULER_VERSION, error, started, trace)
    return legacy


def _success(name: str, result: dict, response_format: str, trace_enabled: bool, started: float, started_at: str, legacy: str, status: str = "SUCCESS"):
    trace = _trace(name, started_at, started, True, status, len(result))
    emit_trace(trace, trace_enabled)
    if response_format == "structured":
        return structured_success(name, _SCHEDULER_VERSION, result, started, trace)
    return legacy


def _build():
    from scheduler.engine import build_scheduler

    return build_scheduler()


@tool(
    name="scheduler_schedule",
    description="Schedule a KING action for later. The action must be on the scheduler whitelist. Optional notes/memory linkage from SCHEDULER_CONFIG.md applies automatically.",
    examples=[
        "schedule a note save for tomorrow at 9 AM",
        "schedule daily_maintenance for tonight 03:30",
        "schedule memory_remember that I prefer dark mode at 2026-05-29T08:00:00",
    ],
    param_descriptions={
        "title": "Short title describing the scheduled work",
        "action": "Tool name to invoke when due (must be whitelisted in SCHEDULER_CONFIG.md)",
        "scheduled_for": "ISO 8601 datetime e.g. 2026-05-29T08:30:00",
        "arguments": "JSON object string with action arguments",
        "tags": "Optional comma-separated tags",
        "response_format": "legacy or structured",
        "trace_enabled": "When true, emit a machine-readable trace entry",
    },
)
def scheduler_schedule(
    title: str,
    action: str,
    scheduled_for: str,
    arguments: str = "{}",
    tags: str = "",
    response_format: str = "legacy",
    trace_enabled: bool = False,
):
    started = time.perf_counter()
    started_at = utc_now_iso()
    response_format = normalize_response_format(response_format)
    trace_enabled = coerce_bool(trace_enabled)
    try:
        parsed_args = json.loads(arguments) if arguments else {}
    except json.JSONDecodeError as exc:
        err = error_payload(
            "INVALID_ARGUMENTS_JSON",
            "arguments must be a JSON object.",
            "arguments",
            arguments,
            "JSON object string",
            False,
            f"Provide valid JSON for arguments. Parser said: {exc.msg}",
        )
        return _error("scheduler_schedule", err, response_format, trace_enabled, started, started_at, "Error: invalid arguments JSON")
    if not isinstance(parsed_args, dict):
        err = error_payload(
            "INVALID_ARGUMENTS_JSON",
            "arguments must be a JSON object.",
            "arguments",
            arguments,
            "JSON object",
            False,
            "Wrap your arguments in {}.",
        )
        return _error("scheduler_schedule", err, response_format, trace_enabled, started, started_at, "Error: arguments must be JSON object")

    scheduler = _build()
    try:
        record = scheduler.schedule(
            title=title,
            action=action,
            scheduled_for=scheduled_for,
            arguments=parsed_args,
            tags=[tag.strip() for tag in (tags or "").split(",") if tag.strip()],
        )
    except ValueError as exc:
        err = error_payload(
            "SCHEDULE_REJECTED",
            str(exc),
            "scheduler",
            None,
            "valid scheduler input",
            False,
            "Check title, action, and scheduled_for, and confirm the action is whitelisted.",
        )
        return _error("scheduler_schedule", err, response_format, trace_enabled, started, started_at, f"Error: {exc}")
    legacy = f"Scheduled '{record['title']}' (id {record['id']}) action={record['action']} for {record['scheduled_for']}"
    return _success("scheduler_schedule", {"item": record}, response_format, trace_enabled, started, started_at, legacy)


@tool(
    name="scheduler_list",
    description="List scheduled items, optionally filtered by status (pending, completed, failed, cancelled, skipped, retry_scheduled).",
    examples=["list my scheduled tasks", "show pending scheduler items"],
    param_descriptions={
        "status": "Optional status filter",
        "response_format": "legacy or structured",
        "trace_enabled": "When true, emit a machine-readable trace entry",
    },
)
def scheduler_list(status: str = "", response_format: str = "legacy", trace_enabled: bool = False):
    started = time.perf_counter()
    started_at = utc_now_iso()
    response_format = normalize_response_format(response_format)
    trace_enabled = coerce_bool(trace_enabled)
    scheduler = _build()
    items = scheduler.list_items(status or None)
    if not items:
        legacy = "No scheduled items"
    else:
        lines = []
        for item in items:
            lines.append(f"- [{item.get('status')}] #{item.get('id')} {item.get('title')} -> {item.get('scheduled_for')} action={item.get('action')}")
        legacy = "\n".join(lines)
    return _success("scheduler_list", {"items": items, "count": len(items), "status_filter": status or None}, response_format, trace_enabled, started, started_at, legacy)


@tool(
    name="scheduler_cancel",
    description="Cancel a scheduled item by id (keeps the record but marks it cancelled).",
    examples=["cancel scheduled item 3", "cancel id 7 in scheduler"],
    param_descriptions={
        "item_id": "Numeric id of the item to cancel",
        "response_format": "legacy or structured",
        "trace_enabled": "When true, emit a machine-readable trace entry",
    },
)
def scheduler_cancel(item_id: int, response_format: str = "legacy", trace_enabled: bool = False):
    started = time.perf_counter()
    started_at = utc_now_iso()
    response_format = normalize_response_format(response_format)
    trace_enabled = coerce_bool(trace_enabled)
    try:
        target_id = int(item_id)
    except (TypeError, ValueError):
        err = error_payload("INVALID_ITEM_ID", "item_id must be an integer.", "item_id", item_id, "integer", False, "Pass an integer id.")
        return _error("scheduler_cancel", err, response_format, trace_enabled, started, started_at, "Error: invalid item_id")
    scheduler = _build()
    ok = scheduler.cancel(target_id)
    legacy = f"Cancelled item {target_id}" if ok else f"No item with id {target_id}"
    return _success("scheduler_cancel", {"cancelled": ok, "item_id": target_id}, response_format, trace_enabled, started, started_at, legacy, status="SUCCESS" if ok else "PARTIAL")


@tool(
    name="scheduler_run_due",
    description="Run any scheduler items that are due now or within the optional horizon (in minutes).",
    examples=["run any due scheduled tasks", "process scheduler items in the next hour"],
    param_descriptions={
        "horizon_minutes": "Optional look-ahead window in minutes (0 means due-only)",
        "response_format": "legacy or structured",
        "trace_enabled": "When true, emit a machine-readable trace entry",
    },
)
def scheduler_run_due(horizon_minutes: int = 0, response_format: str = "legacy", trace_enabled: bool = False):
    started = time.perf_counter()
    started_at = utc_now_iso()
    response_format = normalize_response_format(response_format)
    trace_enabled = coerce_bool(trace_enabled)
    scheduler = _build()
    result = scheduler.run_due(horizon_minutes=int(horizon_minutes or 0))
    legacy = (
        f"Ran {result['ran_count']} item(s); skipped {result['skipped_count']} at {result['checked_at']}"
    )
    return _success("scheduler_run_due", result, response_format, trace_enabled, started, started_at, legacy)


@tool(
    name="daily_maintenance",
    description="Run the KING daily maintenance routine immediately. Idempotent for the current day unless force=true.",
    examples=["run daily maintenance now", "do nightly maintenance for KING"],
    param_descriptions={
        "force": "If true, run even if today's run already happened",
        "dry_run": "If true, plan steps but do not execute handlers",
        "response_format": "legacy or structured",
        "trace_enabled": "When true, emit a machine-readable trace entry",
    },
)
def daily_maintenance_tool(
    force: bool = False,
    dry_run: bool = False,
    response_format: str = "legacy",
    trace_enabled: bool = False,
):
    started = time.perf_counter()
    started_at = utc_now_iso()
    response_format = normalize_response_format(response_format)
    trace_enabled = coerce_bool(trace_enabled)
    from maintenance.engine import build_engine
    from maintenance.steps import register_default_steps

    engine = build_engine()
    register_default_steps(engine)
    result = engine.run(triggered_by="tool_call", dry_run=coerce_bool(dry_run), force=coerce_bool(force))
    payload = result.to_dict()
    legacy = f"Daily maintenance status={payload['status']} steps={len(payload['steps'])}"
    return _success("daily_maintenance", payload, response_format, trace_enabled, started, started_at, legacy)
