import time

from telegram_watcher.client import ensure_service_for_tool, send_cli_message
from tools.registry import tool
from tools.runtime import (
    coerce_bool,
    emit_trace,
    error_payload,
    make_trace,
    normalize_response_format,
    normalize_timeout_ms,
    structured_error,
    structured_success,
    utc_now_iso,
)

_TELEGRAM_WATCHER_TOOL_VERSION = "1.0.0"
_ACTIONS = {
    "status",
    "health",
    "latest",
    "find",
    "search",
    "send",
    "sendfile",
    "info",
    "new",
    "list",
    "watch_on",
    "watch_off",
    "message",
}


def _trace(started_at: str, started: float, inputs_received: int, path: str, status: str, fields: int, error_code: str | None = None) -> dict:
    return make_trace(
        "telegram_watcher",
        _TELEGRAM_WATCHER_TOOL_VERSION,
        started_at,
        started,
        inputs_received,
        True,
        path,
        status,
        fields,
        {"count": 1, "systems": ["telegram_watcher"]},
        error_code,
    )


def _tool_error(error: dict, response_format: str, trace_enabled: bool, started: float, started_at: str, legacy: str):
    trace = _trace(started_at, started, 6, "validate", "FAILED", 1, error["code"])
    emit_trace(trace, trace_enabled)
    if response_format == "structured":
        return structured_error("telegram_watcher", _TELEGRAM_WATCHER_TOOL_VERSION, error, started, trace)
    return legacy


def _tool_success(result: dict, response_format: str, trace_enabled: bool, started: float, started_at: str, legacy: str, path: str, status: str = "SUCCESS"):
    trace = _trace(started_at, started, 6, path, status, len(result))
    emit_trace(trace, trace_enabled)
    if response_format == "structured":
        return structured_success("telegram_watcher", _TELEGRAM_WATCHER_TOOL_VERSION, result, started, trace)
    return legacy


def _request_text(action: str, message: str) -> str:
    text = str(message or "").strip()
    if action == "message":
        return text
    if text:
        return f"{action} {text}".strip()
    return action


@tool(
    name="telegram_watcher",
    description="Use the background Telegram watcher courier service for allowed-zone file delivery, status, health, recent files, search, and watch notifications",
    examples=[
        "check telegram watcher status",
        "send the latest report through telegram",
        "find allowed telegram files about invoices",
        "turn telegram file watch on",
    ],
    param_descriptions={
        "action": "status, health, latest, find, search, send, sendfile, info, new, list, watch_on, watch_off, or message",
        "message": "Natural target text for the watcher action",
        "config_path": "Optional markdown config path. Defaults to KING_TELEGRAM_CONFIG_FILE or tools/TELEGRAM_WATCHER_CONFIG.md",
        "response_format": "legacy or structured",
        "trace_enabled": "Emit machine-readable trace when true",
        "timeout_ms": "Service request timeout in milliseconds",
    },
)
def telegram_watcher(
    action: str = "status",
    message: str = "",
    config_path: str = "",
    response_format: str = "legacy",
    trace_enabled: bool = False,
    timeout_ms: int = 15000,
):
    started = time.perf_counter()
    started_at = utc_now_iso()
    response_format = normalize_response_format(response_format)
    trace_enabled = coerce_bool(trace_enabled)
    action = str(action or "status").strip().casefold()
    if action not in _ACTIONS:
        error = error_payload(
            "INVALID_ACTION",
            "action is not supported by the Telegram watcher tool.",
            "action",
            action,
            ", ".join(sorted(_ACTIONS)),
            False,
            "Use one of the configured Telegram watcher actions.",
        )
        return _tool_error(error, response_format, trace_enabled, started, started_at, "Telegram watcher action is invalid")
    timeout, timeout_error = normalize_timeout_ms(timeout_ms, default_ms=15000)
    if timeout_error is not None:
        return _tool_error(timeout_error, response_format, trace_enabled, started, started_at, "Telegram watcher timeout is invalid")

    config, service_status = ensure_service_for_tool(config_path or None)
    result = {
        "action": action,
        "service_status": service_status,
        "service_base_url": config.service_base_url,
    }
    state = str(service_status.get("status") or "")
    if state not in {"running", "started"}:
        reason = str(service_status.get("reason") or state or "unavailable")
        result["status"] = "disabled" if state == "disabled" else "unavailable"
        result["reason"] = reason
        legacy = "Telegram watcher is not available: " + reason
        return _tool_success(result, response_format, trace_enabled, started, started_at, legacy, "service_status", "PARTIAL")

    text = _request_text(action, message)
    if not text:
        error = error_payload(
            "EMPTY_MESSAGE",
            "message is required when action is message.",
            "message",
            message,
            "non-empty message",
            False,
            "Pass a natural Telegram watcher request.",
        )
        return _tool_error(error, response_format, trace_enabled, started, started_at, "Telegram watcher message is empty")

    local_result = send_cli_message(config, text, session_id="king_tool")
    result["request"] = text
    result["local_result"] = local_result
    result["handled"] = bool(local_result.get("handled"))
    result["status"] = str(local_result.get("status") or "unknown")
    result["text"] = str(local_result.get("text") or "").strip()
    result["documents"] = local_result.get("documents") if isinstance(local_result.get("documents"), list) else []
    legacy = result["text"] or ("Telegram watcher did not handle this request: " + result["status"])
    status = "SUCCESS" if result["handled"] else "PARTIAL"
    return _tool_success(result, response_format, trace_enabled, started, started_at, legacy, "service_request", status)
