"""System Pulse: live machine vitals.

Gives KING real-time awareness of the machine it runs on: CPU, memory, battery,
disk, uptime, and the heaviest processes. This is the "status report" a JARVIS
should be able to give instantly. Read-only. Uses psutil; degrades to a
structured error when unavailable.
"""

import time
from datetime import datetime

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

_PULSE_VERSION = "1.0.0"


def _trace(name, started_at, started, schema_valid, status, output_fields, error_code=None):
    return make_trace(
        name, _PULSE_VERSION, started_at, started, 1, schema_valid,
        "system_pulse", status, output_fields, {"count": 1, "systems": ["os_metrics"]}, error_code,
    )


def _emit(name, started, started_at, trace_enabled, result=None, error=None, response_format="legacy", legacy="", status="SUCCESS"):
    valid = error is None
    trace = _trace(name, started_at, started, valid, status if valid else "FAILED",
                   len(result) if result else 1, None if valid else error["code"])
    emit_trace(trace, trace_enabled)
    if response_format == "structured":
        if valid:
            return structured_success(name, _PULSE_VERSION, result, started, trace)
        return structured_error(name, _PULSE_VERSION, error, started, trace)
    return legacy


def _top_processes(psutil, limit: int) -> list[dict]:
    procs = []
    for proc in psutil.process_iter(["pid", "name", "memory_percent"]):
        try:
            info = proc.info
            procs.append({"pid": info.get("pid"), "name": info.get("name") or "?", "memory_percent": round(float(info.get("memory_percent") or 0.0), 1)})
        except Exception:
            continue
    procs.sort(key=lambda item: item["memory_percent"], reverse=True)
    return procs[:limit]


@tool(
    name="system_pulse",
    description="Report live machine vitals: CPU load, memory, battery, disk usage, uptime, and the top memory-using processes.",
    examples=[
        "how's my system doing",
        "show cpu and memory usage",
        "what's eating my ram",
        "battery status",
    ],
    param_descriptions={
        "top_n": "How many top processes to include, 0 to 10",
        "response_format": "legacy or structured",
        "trace_enabled": "When true, emit a machine-readable trace entry",
    },
)
def system_pulse(top_n: int = 5, response_format: str = "legacy", trace_enabled: bool = False):
    started = time.perf_counter()
    started_at = utc_now_iso()
    response_format = normalize_response_format(response_format)
    trace_enabled = coerce_bool(trace_enabled)
    top_n, top_err = normalize_int(top_n, "top_n", 5, 0, 10, "Use top_n between 0 and 10.", "INVALID_TOP_N")
    if top_err is not None:
        return _emit("system_pulse", started, started_at, trace_enabled, error=top_err, response_format=response_format, legacy="Error: invalid top_n", status="FAILED")

    try:
        import psutil
    except Exception:
        err = error_payload("PSUTIL_UNAVAILABLE", "psutil is not available.", "system_pulse", None, "psutil installed", True, "Install psutil to enable system metrics.")
        return _emit("system_pulse", started, started_at, trace_enabled, error=err, response_format=response_format, legacy="Error: system metrics backend unavailable", status="FAILED")

    cpu_percent = psutil.cpu_percent(interval=0.1)
    mem = psutil.virtual_memory()
    disk = psutil.disk_usage("/")
    boot = psutil.boot_time()
    uptime_seconds = max(0, int(time.time() - boot))
    battery_info = None
    try:
        battery = psutil.sensors_battery()
        if battery is not None:
            battery_info = {"percent": round(battery.percent, 1), "plugged": bool(battery.power_plugged)}
    except Exception:
        battery_info = None

    result = {
        "cpu_percent": cpu_percent,
        "memory": {"percent": mem.percent, "used_gb": round(mem.used / 1e9, 2), "total_gb": round(mem.total / 1e9, 2)},
        "disk": {"percent": disk.percent, "used_gb": round(disk.used / 1e9, 2), "total_gb": round(disk.total / 1e9, 2)},
        "battery": battery_info,
        "uptime_hours": round(uptime_seconds / 3600, 1),
        "top_processes": _top_processes(psutil, top_n) if top_n else [],
        "captured_at": datetime.now().isoformat(timespec="seconds"),
    }

    battery_text = ""
    if battery_info is not None:
        battery_text = f" Battery {battery_info['percent']}%{' (charging)' if battery_info['plugged'] else ''}."
    legacy = (
        f"CPU {cpu_percent}% | RAM {mem.percent}% ({result['memory']['used_gb']}/{result['memory']['total_gb']} GB) | "
        f"Disk {disk.percent}% | Up {result['uptime_hours']}h.{battery_text}"
    )
    if result["top_processes"]:
        top = ", ".join(f"{p['name']} {p['memory_percent']}%" for p in result["top_processes"])
        legacy += f" Top: {top}."
    return _emit("system_pulse", started, started_at, trace_enabled, result=result, response_format=response_format, legacy=legacy)
