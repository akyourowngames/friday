import json
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

_TRACE_LOG = Path(__file__).resolve().parent.parent / "storage" / "tool_traces.jsonl"


RESPONSE_FORMAT_LEGACY = "legacy"
RESPONSE_FORMAT_STRUCTURED = "structured"
RESPONSE_FORMATS = (RESPONSE_FORMAT_LEGACY, RESPONSE_FORMAT_STRUCTURED)
MAX_TIMEOUT_MS = 60000


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def coerce_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("1", "true", "yes", "on"):
            return True
        if lowered in ("0", "false", "no", "off"):
            return False
    return bool(value)


def normalize_response_format(value: str) -> str:
    if value in RESPONSE_FORMATS:
        return value
    return RESPONSE_FORMAT_LEGACY


def error_payload(code: str, message: str, field: str, value, expected: str, retryable: bool, suggestion: str) -> dict:
    return {
        "code": code,
        "message": message,
        "field": field,
        "value": value,
        "expected": expected,
        "retryable": retryable,
        "suggestion": suggestion,
    }


def normalize_int(
    value,
    field: str,
    default: int,
    minimum: int,
    maximum: int,
    suggestion: str,
    code: str = "INVALID_INTEGER",
):
    if value is None:
        return default, None
    if isinstance(value, bool):
        return None, error_payload(
            code,
            f"{field} must be an integer between {minimum} and {maximum}.",
            field,
            value,
            f"integer {minimum}..{maximum}",
            False,
            suggestion,
        )
    try:
        normalized = int(value)
    except (TypeError, ValueError):
        return None, error_payload(
            code,
            f"{field} must be an integer between {minimum} and {maximum}.",
            field,
            value,
            f"integer {minimum}..{maximum}",
            False,
            suggestion,
        )
    if normalized < minimum or normalized > maximum:
        return None, error_payload(
            code,
            f"{field} is outside the supported range.",
            field,
            value,
            f"integer {minimum}..{maximum}",
            False,
            suggestion,
        )
    return normalized, None


def normalize_timeout_ms(value, default_ms: int | None = None, maximum: int = MAX_TIMEOUT_MS):
    if value in (None, 0, "0", ""):
        return default_ms, None
    if isinstance(value, bool):
        return None, error_payload(
            "INVALID_TIMEOUT",
            "timeout_ms must be an integer between 1 and 60000.",
            "timeout_ms",
            value,
            "integer 1..60000",
            False,
            "Pass a timeout between 1 and 60000 milliseconds, or omit timeout_ms.",
        )
    try:
        normalized = int(value)
    except (TypeError, ValueError):
        return None, error_payload(
            "INVALID_TIMEOUT",
            "timeout_ms must be an integer between 1 and 60000.",
            "timeout_ms",
            value,
            "integer 1..60000",
            False,
            "Pass a timeout between 1 and 60000 milliseconds, or omit timeout_ms.",
        )
    if normalized < 1 or normalized > maximum:
        return None, error_payload(
            "INVALID_TIMEOUT",
            "timeout_ms is outside the supported range.",
            "timeout_ms",
            value,
            f"integer 1..{maximum}",
            False,
            "Use a timeout between 1 and 60000 milliseconds.",
        )
    return normalized, None


def meta(tool_name: str, version: str, started: float, attempt: int = 1) -> dict:
    return {
        "tool": tool_name,
        "version": version,
        "duration_ms": int((time.perf_counter() - started) * 1000),
        "attempt": attempt,
        "timestamp": utc_now_iso(),
    }


def make_trace(
    tool_name: str,
    version: str,
    started_at: str,
    started: float,
    inputs_received: int,
    schema_valid: bool,
    execution_path: str,
    status: str,
    output_fields: int,
    external_calls: dict | None = None,
    error_code: str | None = None,
) -> dict:
    return {
        "event": "TOOL TRACE",
        "tool": tool_name,
        "version": version,
        "call_id": str(uuid.uuid4()),
        "started_at": started_at,
        "inputs_received": inputs_received,
        "schema_valid": "YES" if schema_valid else "NO",
        "execution_path": execution_path,
        "external_calls": external_calls or {"count": 0, "systems": []},
        "duration_ms": int((time.perf_counter() - started) * 1000),
        "output_fields": output_fields,
        "status": status,
        "error_code": error_code,
    }


def emit_trace(trace: dict, enabled: bool) -> None:
    if enabled:
        try:
            _TRACE_LOG.parent.mkdir(parents=True, exist_ok=True)
            with _TRACE_LOG.open("a", encoding="utf-8") as f:
                f.write(json.dumps(trace, sort_keys=True) + "\n")
        except Exception:
            pass


def structured_success(tool_name: str, version: str, result: dict, started: float, trace: dict | None = None) -> dict:
    response = {
        "result": result,
        "meta": meta(tool_name, version, started),
    }
    if trace is not None:
        response["trace"] = trace
    return response


def structured_error(tool_name: str, version: str, error: dict, started: float, trace: dict | None = None) -> dict:
    response = {
        "error": error,
        "meta": meta(tool_name, version, started),
    }
    if trace is not None:
        response["trace"] = trace
    return response
