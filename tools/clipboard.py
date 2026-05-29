"""Clipboard tool: read and write the system clipboard.

A desktop operator needs to move text in and out of the clipboard. Uses
pyperclip when available, degrading to a clear structured error otherwise
(never a false claim of success).
"""

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

_CLIP_VERSION = "1.0.0"


def _trace(name, started_at, started, schema_valid, status, output_fields, error_code=None):
    return make_trace(
        name, _CLIP_VERSION, started_at, started, 1, schema_valid,
        "clipboard", status, output_fields, {"count": 1, "systems": ["system_clipboard"]}, error_code,
    )


def _emit(name, started, started_at, trace_enabled, result=None, error=None, response_format="legacy", legacy="", status="SUCCESS"):
    valid = error is None
    trace = _trace(name, started_at, started, valid, status if valid else "FAILED",
                   len(result) if result else 1, None if valid else error["code"])
    emit_trace(trace, trace_enabled)
    if response_format == "structured":
        if valid:
            return structured_success(name, _CLIP_VERSION, result, started, trace)
        return structured_error(name, _CLIP_VERSION, error, started, trace)
    return legacy


def _backend():
    try:
        import pyperclip

        return pyperclip
    except Exception:
        return None


@tool(
    name="clipboard",
    description="Read the current system clipboard text, or copy new text into it. action is 'read' or 'write'.",
    examples=[
        "what's on my clipboard",
        "copy this to clipboard: hello world",
        "read clipboard",
    ],
    param_descriptions={
        "action": "read or write",
        "text": "Text to copy when action is write",
        "response_format": "legacy or structured",
        "trace_enabled": "When true, emit a machine-readable trace entry",
    },
)
def clipboard(action: str = "read", text: str = "", response_format: str = "legacy", trace_enabled: bool = False):
    started = time.perf_counter()
    started_at = utc_now_iso()
    response_format = normalize_response_format(response_format)
    trace_enabled = coerce_bool(trace_enabled)
    act = str(action or "read").strip().lower()

    backend = _backend()
    if backend is None:
        err = error_payload("CLIPBOARD_UNAVAILABLE", "No clipboard backend is available.", "clipboard", None, "pyperclip installed", True, "Install pyperclip to enable clipboard access.")
        return _emit("clipboard", started, started_at, trace_enabled, error=err, response_format=response_format, legacy="Error: clipboard backend unavailable", status="FAILED")

    if act == "write":
        payload = str(text or "")
        if not payload:
            err = error_payload("EMPTY_TEXT", "text must not be empty for write.", "text", text, "non-empty text", False, "Provide text to copy.")
            return _emit("clipboard", started, started_at, trace_enabled, error=err, response_format=response_format, legacy="Error: nothing to copy", status="FAILED")
        try:
            backend.copy(payload)
        except Exception as exc:
            err = error_payload("CLIPBOARD_WRITE_FAILED", f"{type(exc).__name__}", "clipboard", None, "writable clipboard", True, "Retry; ensure no other app is locking the clipboard.")
            return _emit("clipboard", started, started_at, trace_enabled, error=err, response_format=response_format, legacy="Error: clipboard write failed", status="FAILED")
        result = {"action": "write", "length": len(payload)}
        return _emit("clipboard", started, started_at, trace_enabled, result=result, response_format=response_format, legacy=f"Copied {len(payload)} chars to the clipboard.")

    # read
    try:
        content = backend.paste() or ""
    except Exception as exc:
        err = error_payload("CLIPBOARD_READ_FAILED", f"{type(exc).__name__}", "clipboard", None, "readable clipboard", True, "Retry clipboard read.")
        return _emit("clipboard", started, started_at, trace_enabled, error=err, response_format=response_format, legacy="Error: clipboard read failed", status="FAILED")
    result = {"action": "read", "content": content, "length": len(content), "empty": not content}
    legacy = content if content else "Clipboard is empty."
    return _emit("clipboard", started, started_at, trace_enabled, result=result, response_format=response_format, legacy=legacy)
