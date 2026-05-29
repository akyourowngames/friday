"""Screenshot tool: capture the screen to the images directory.

Pairs with the existing camera vision tool: capture now, inspect later. Uses
Pillow's ImageGrab on Windows, degrading to a structured error when capture is
unavailable.
"""

import time
from datetime import datetime
from pathlib import Path

from config import settings
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

_SHOT_VERSION = "1.0.0"


def _images_dir() -> Path:
    path = Path(settings.images_dir)
    if not path.is_absolute():
        path = Path(__file__).resolve().parent.parent / path
    return path


def _trace(name, started_at, started, schema_valid, status, output_fields, error_code=None):
    return make_trace(
        name, _SHOT_VERSION, started_at, started, 1, schema_valid,
        "screenshot", status, output_fields, {"count": 1, "systems": ["display"]}, error_code,
    )


def _emit(name, started, started_at, trace_enabled, result=None, error=None, response_format="legacy", legacy="", status="SUCCESS"):
    valid = error is None
    trace = _trace(name, started_at, started, valid, status if valid else "FAILED",
                   len(result) if result else 1, None if valid else error["code"])
    emit_trace(trace, trace_enabled)
    if response_format == "structured":
        if valid:
            return structured_success(name, _SHOT_VERSION, result, started, trace)
        return structured_error(name, _SHOT_VERSION, error, started, trace)
    return legacy


@tool(
    name="screenshot",
    description="Capture the current screen to an image file in the KING images directory.",
    examples=[
        "take a screenshot",
        "capture my screen",
        "grab a screenshot of what's on screen",
    ],
    param_descriptions={
        "label": "Optional short label used in the filename",
        "response_format": "legacy or structured",
        "trace_enabled": "When true, emit a machine-readable trace entry",
    },
)
def screenshot(label: str = "", response_format: str = "legacy", trace_enabled: bool = False):
    started = time.perf_counter()
    started_at = utc_now_iso()
    response_format = normalize_response_format(response_format)
    trace_enabled = coerce_bool(trace_enabled)

    try:
        from PIL import ImageGrab
    except Exception:
        err = error_payload("CAPTURE_UNAVAILABLE", "Screen capture backend is not available.", "screenshot", None, "Pillow with ImageGrab", True, "Install Pillow to enable screenshots.")
        return _emit("screenshot", started, started_at, trace_enabled, error=err, response_format=response_format, legacy="Error: screenshot backend unavailable", status="FAILED")

    try:
        image = ImageGrab.grab()
    except Exception as exc:
        err = error_payload("CAPTURE_FAILED", f"{type(exc).__name__}", "screenshot", None, "captured frame", True, "Retry; ensure a desktop session is available.")
        return _emit("screenshot", started, started_at, trace_enabled, error=err, response_format=response_format, legacy="Error: screen capture failed", status="FAILED")

    images_dir = _images_dir()
    images_dir.mkdir(parents=True, exist_ok=True)
    safe_label = "".join(ch if ch.isalnum() else "_" for ch in str(label or "").strip()).strip("_")[:40]
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"screenshot_{stamp}{('_' + safe_label) if safe_label else ''}.png"
    output = images_dir / filename
    try:
        image.save(output)
    except Exception as exc:
        err = error_payload("SAVE_FAILED", f"{type(exc).__name__}", "screenshot", None, "writable images dir", True, "Check images directory permissions.")
        return _emit("screenshot", started, started_at, trace_enabled, error=err, response_format=response_format, legacy="Error: could not save screenshot", status="FAILED")

    result = {"path": str(output), "filename": filename, "width": image.width, "height": image.height, "size_bytes": output.stat().st_size}
    return _emit("screenshot", started, started_at, trace_enabled, result=result, response_format=response_format, legacy=f"Screenshot saved: {output} ({image.width}x{image.height}).")
