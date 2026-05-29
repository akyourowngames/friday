"""Process control: find processes and optionally terminate them.

Read-first: the default action lists matching processes. Termination requires an
explicit action and a confirmed match, so KING never kills something on a vague
request. Uses psutil. Matching is by case-insensitive substring on the process
name (token comparison, not regex).
"""

import time

from tools.registry import tool
from tools.runtime import (
    coerce_bool,
    emit_trace,
    error_payload,
    make_trace,
    normalize_int,
    normalize_response_format,
    structured_error,
    structured_success,
    utc_now_iso,
)

_PROC_VERSION = "1.0.0"


def _trace(name, started_at, started, schema_valid, status, output_fields, error_code=None):
    return make_trace(
        name, _PROC_VERSION, started_at, started, 1, schema_valid,
        "process_control", status, output_fields, {"count": 1, "systems": ["os_process_table"]}, error_code,
    )


def _emit(name, started, started_at, trace_enabled, result=None, error=None, response_format="legacy", legacy="", status="SUCCESS"):
    valid = error is None
    trace = _trace(name, started_at, started, valid, status if valid else "FAILED",
                   len(result) if result else 1, None if valid else error["code"])
    emit_trace(trace, trace_enabled)
    if response_format == "structured":
        if valid:
            return structured_success(name, _PROC_VERSION, result, started, trace)
        return structured_error(name, _PROC_VERSION, error, started, trace)
    return legacy


def _find(psutil, needle: str, limit: int) -> list[dict]:
    needle_low = needle.lower()
    found = []
    for proc in psutil.process_iter(["pid", "name"]):
        try:
            pname = str(proc.info.get("name") or "")
            if needle_low in pname.lower():
                found.append({"pid": proc.info.get("pid"), "name": pname})
        except Exception:
            continue
        if len(found) >= limit:
            break
    return found


@tool(
    name="process_control",
    description="Find running processes by name (action 'find'), or terminate matching processes (action 'terminate'). Termination acts only on confirmed name matches.",
    examples=[
        "find chrome processes",
        "is notepad running",
        "terminate notepad",
    ],
    param_descriptions={
        "name": "Process name or substring to match",
        "action": "find or terminate",
        "max_matches": "Maximum matches to act on, 1 to 50",
        "response_format": "legacy or structured",
        "trace_enabled": "When true, emit a machine-readable trace entry",
    },
)
def process_control(name: str, action: str = "find", max_matches: int = 20, response_format: str = "legacy", trace_enabled: bool = False):
    started = time.perf_counter()
    started_at = utc_now_iso()
    response_format = normalize_response_format(response_format)
    trace_enabled = coerce_bool(trace_enabled)
    act = str(action or "find").strip().lower()

    needle = str(name or "").strip()
    if not needle:
        err = error_payload("EMPTY_NAME", "name must not be empty.", "name", name, "process name or substring", False, "Provide a process name to match.")
        return _emit("process_control", started, started_at, trace_enabled, error=err, response_format=response_format, legacy="Error: process name is required", status="FAILED")

    max_matches, max_err = normalize_int(max_matches, "max_matches", 20, 1, 50, "Use max_matches between 1 and 50.", "INVALID_MAX_MATCHES")
    if max_err is not None:
        return _emit("process_control", started, started_at, trace_enabled, error=max_err, response_format=response_format, legacy="Error: invalid max_matches", status="FAILED")

    try:
        import psutil
    except Exception:
        err = error_payload("PSUTIL_UNAVAILABLE", "psutil is not available.", "process_control", None, "psutil installed", True, "Install psutil to enable process control.")
        return _emit("process_control", started, started_at, trace_enabled, error=err, response_format=response_format, legacy="Error: process backend unavailable", status="FAILED")

    matches = _find(psutil, needle, max_matches)

    if act == "find":
        result = {"action": "find", "name": needle, "matches": matches, "count": len(matches)}
        if not matches:
            return _emit("process_control", started, started_at, trace_enabled, result=result, response_format=response_format, legacy=f"No running process matches '{needle}'.", status="PARTIAL")
        listing = ", ".join(f"{m['name']} (pid {m['pid']})" for m in matches)
        return _emit("process_control", started, started_at, trace_enabled, result=result, response_format=response_format, legacy=f"Found {len(matches)} match(es): {listing}.")

    if act == "terminate":
        if not matches:
            result = {"action": "terminate", "name": needle, "terminated": [], "count": 0}
            return _emit("process_control", started, started_at, trace_enabled, result=result, response_format=response_format, legacy=f"Nothing to terminate; no process matches '{needle}'.", status="PARTIAL")
        terminated = []
        failures = []
        for match in matches:
            try:
                proc = psutil.Process(match["pid"])
                proc.terminate()
                terminated.append(match)
            except Exception as exc:
                failures.append({"pid": match["pid"], "error": type(exc).__name__})
        result = {"action": "terminate", "name": needle, "terminated": terminated, "failures": failures, "count": len(terminated)}
        legacy = f"Terminated {len(terminated)} process(es) matching '{needle}'."
        if failures:
            legacy += f" {len(failures)} could not be terminated."
        status = "SUCCESS" if terminated else "PARTIAL"
        return _emit("process_control", started, started_at, trace_enabled, result=result, response_format=response_format, legacy=legacy, status=status)

    err = error_payload("INVALID_ACTION", "action must be 'find' or 'terminate'.", "action", action, "find|terminate", False, "Use action 'find' or 'terminate'.")
    return _emit("process_control", started, started_at, trace_enabled, error=err, response_format=response_format, legacy="Error: invalid action", status="FAILED")
