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

_DATETIME_VERSION = "2.0.0"
_OUTPUT_STYLES = ("human", "iso")


def _timezone_matches(timezone: str, zoneinfo) -> list[str]:
    target = timezone.strip().replace(" ", "_").lower()
    if not target:
        return []

    exact = []
    partial = []
    for zone in sorted(zoneinfo.available_timezones()):
        zone_key = zone.lower()
        city_key = zone.rsplit("/", 1)[-1].lower()
        if target == zone_key or target == city_key:
            exact.append(zone)
        elif target in zone_key or target in city_key:
            partial.append(zone)
    return exact or partial


def _resolve_timezone(timezone: str, zoneinfo) -> tuple[object | None, str, str | None, list[str]]:
    import zoneinfo as zi

    norm = str(timezone or "").strip()
    if norm.lower() in ("local", "", "here"):
        tz = datetime.now().astimezone().tzinfo
        return tz, "local", None, []

    try:
        tz = zi.ZoneInfo(norm)
        return tz, norm, None, []
    except (zi.ZoneInfoNotFoundError, OSError, ValueError):
        matches = _timezone_matches(norm, zoneinfo)
        if len(matches) == 1:
            resolved = matches[0]
            return zi.ZoneInfo(resolved), resolved, None, matches
        if matches:
            return None, norm, "ambiguous", matches
        return None, norm, "unknown", []


def _datetime_trace(
    started_at: str,
    started: float,
    inputs_received: int,
    schema_valid: bool,
    execution_path: str,
    status: str,
    output_fields: int,
    error_code: str | None = None,
) -> dict:
    return make_trace(
        "datetime_info",
        _DATETIME_VERSION,
        started_at,
        started,
        inputs_received,
        schema_valid,
        execution_path,
        status,
        output_fields,
        {"count": 0, "systems": []},
        error_code,
    )


def _datetime_error(
    error: dict,
    response_format: str,
    trace_enabled: bool,
    started: float,
    started_at: str,
    inputs_received: int,
    legacy: str,
    execution_path: str = "resolve",
):
    trace = _datetime_trace(started_at, started, inputs_received, False, execution_path, "FAILED", 1, error["code"])
    emit_trace(trace, trace_enabled)
    if response_format == "structured":
        return structured_error("datetime_info", _DATETIME_VERSION, error, started, trace)
    return legacy


def _build_result(now: datetime, resolved: str, output_style: str) -> dict:
    offset = now.utcoffset()
    offset_seconds = int(offset.total_seconds()) if offset is not None else 0
    offset_label = now.strftime("%z")
    if output_style == "iso":
        display = now.isoformat(timespec="seconds")
    else:
        display = f"{now.strftime('%A, %B %d, %Y  %I:%M:%S %p  %Z')} ({resolved})"
    return {
        "timezone_requested": resolved if resolved != "local" else "local",
        "timezone_resolved": resolved,
        "display": display,
        "iso": now.isoformat(timespec="seconds"),
        "unix_timestamp": int(now.timestamp()),
        "weekday": now.strftime("%A"),
        "date": now.strftime("%Y-%m-%d"),
        "time_24h": now.strftime("%H:%M:%S"),
        "utc_offset_seconds": offset_seconds,
        "utc_offset": offset_label,
        "is_dst": bool(now.dst()) if now.dst() is not None else False,
        "output_style": output_style,
    }


@tool(
    name="datetime_info",
    description="Get current date and time for any timezone",
    examples=[
        "what time in Tokyo",
        "current time in London",
        "what's the date in New York",
        "time in Asia/Shanghai",
    ],
    param_descriptions={
        "timezone": "IANA timezone, city name, or local",
        "output_style": "human (default) or iso for machine-readable timestamp",
        "response_format": "legacy or structured. Default legacy preserves existing output",
        "trace_enabled": "When true, emit a machine-readable trace entry",
    },
)
def datetime_info(
    timezone: str = "local",
    output_style: str = "human",
    response_format: str = "legacy",
    trace_enabled: bool = False,
):
    import zoneinfo

    started = time.perf_counter()
    started_at = utc_now_iso()
    inputs_received = 4
    response_format = normalize_response_format(response_format)
    trace_enabled = coerce_bool(trace_enabled)
    output_style = str(output_style or "human").strip().lower()
    if output_style not in _OUTPUT_STYLES:
        error = error_payload(
            "INVALID_OUTPUT_STYLE",
            "output_style must be human or iso.",
            "output_style",
            output_style,
            "human or iso",
            False,
            "Use output_style='human' for readable text or output_style='iso' for ISO-8601.",
        )
        return _datetime_error(
            error,
            response_format,
            trace_enabled,
            started,
            started_at,
            inputs_received,
            f"Unknown output_style: '{output_style}'. Use human or iso.",
            "input_validation",
        )

    try:
        tz, resolved, problem, matches = _resolve_timezone(timezone, zoneinfo)
        if problem == "ambiguous":
            shown = ", ".join(matches[:8])
            more = f" (+{len(matches) - 8} more)" if len(matches) > 8 else ""
            legacy = f"Ambiguous timezone '{timezone}'. Matches: {shown}{more}"
            if response_format == "structured":
                result = {
                    "status": "ambiguous",
                    "timezone_requested": timezone,
                    "matches": matches[:20],
                    "match_count": len(matches),
                    "error": error_payload(
                        "AMBIGUOUS_TIMEZONE",
                        "Multiple timezones matched the requested name.",
                        "timezone",
                        timezone,
                        "single IANA timezone",
                        False,
                        "Use a full IANA name such as Asia/Tokyo.",
                    ),
                }
                trace = _datetime_trace(started_at, started, inputs_received, True, "resolve", "PARTIAL", len(result), "AMBIGUOUS_TIMEZONE")
                emit_trace(trace, trace_enabled)
                return structured_success("datetime_info", _DATETIME_VERSION, result, started, trace)
            return legacy
        if problem == "unknown" or tz is None:
            legacy = f"Unknown timezone: '{timezone}'. Try a city like 'Asia/Tokyo' or 'America/New_York'."
            error = error_payload(
                "UNKNOWN_TIMEZONE",
                "The requested timezone could not be resolved.",
                "timezone",
                timezone,
                "valid IANA timezone or city",
                False,
                "Try Asia/Tokyo, America/New_York, or local.",
            )
            return _datetime_error(error, response_format, trace_enabled, started, started_at, inputs_received, legacy)

        now = datetime.now(tz)
        result = _build_result(now, resolved, output_style)
        legacy = result["display"] if output_style == "human" else result["iso"]
        trace = _datetime_trace(started_at, started, inputs_received, True, "resolve", "SUCCESS", len(result))
        emit_trace(trace, trace_enabled)
        if response_format == "structured":
            return structured_success("datetime_info", _DATETIME_VERSION, result, started, trace)
        return legacy
    except Exception as e:
        error = error_payload(
            "DATETIME_FAILED",
            "Datetime lookup failed before completion.",
            "timezone",
            timezone,
            "successful timezone resolution",
            True,
            "Retry with a valid timezone name.",
        )
        return _datetime_error(
            error,
            response_format,
            trace_enabled,
            started,
            started_at,
            inputs_received,
            f"Error: {e.__class__.__name__}",
            "resolve",
        )
