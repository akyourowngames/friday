import math
import time

import httpx

from config import settings
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


_NAVIGATOR_VERSION = "1.0.0"
_ROUTE_MODES = ("driving", "walking", "cycling")


def _navigator_trace(
    started_at: str,
    started: float,
    inputs_received: int,
    schema_valid: bool,
    execution_path: str,
    status: str,
    output_fields: int,
    external_count: int,
    error_code: str | None = None,
) -> dict:
    return make_trace(
        "navigator",
        _NAVIGATOR_VERSION,
        started_at,
        started,
        inputs_received,
        schema_valid,
        execution_path,
        status,
        output_fields,
        {
            "count": external_count,
            "systems": ["nominatim", "osrm"] if external_count else [],
        },
        error_code,
    )


def _navigator_error(
    error: dict,
    response_format: str,
    trace_enabled: bool,
    started: float,
    started_at: str,
    inputs_received: int,
    legacy: str,
    execution_path: str = "input_validation",
    external_count: int = 0,
):
    trace = _navigator_trace(
        started_at,
        started,
        inputs_received,
        False,
        execution_path,
        "FAILED",
        1,
        external_count,
        error["code"],
    )
    emit_trace(trace, trace_enabled)
    if response_format == "structured":
        return structured_error("navigator", _NAVIGATOR_VERSION, error, started, trace)
    return legacy


def _provider_attempt(operation, attempts: int | None = None, delay: float | None = None) -> tuple[object | None, str]:
    attempts = max(1, int(attempts if attempts is not None else settings.external_request_attempts))
    delay = max(0.0, float(delay if delay is not None else settings.external_retry_delay))
    last_error = "provider unavailable"
    for attempt in range(attempts):
        try:
            return operation(), ""
        except httpx.TimeoutException:
            last_error = "timeout"
        except httpx.HTTPStatusError as exc:
            response = getattr(exc, "response", None)
            status = response.status_code if response is not None else "unknown"
            last_error = f"http {status}"
            if isinstance(status, int) and 400 <= status < 500:
                break
        except httpx.HTTPError as exc:
            last_error = exc.__class__.__name__
        except Exception as exc:
            last_error = exc.__class__.__name__
        if attempt < attempts - 1 and delay:
            time.sleep(delay)
    return None, f"{last_error} after {attempts} attempt(s)"


def _headers() -> dict:
    return {
        "User-Agent": settings.navigator_user_agent,
        "Accept": "application/json",
    }


def _as_float(value, fallback: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback


def _geocode(query: str, timeout_seconds: float) -> tuple[dict | None, str]:
    def run():
        response = httpx.get(
            settings.navigator_geocode_url,
            params={
                "q": query,
                "format": "jsonv2",
                "limit": 1,
                "addressdetails": 1,
            },
            timeout=timeout_seconds,
            headers=_headers(),
        )
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, list) or not data:
            return None
        item = data[0]
        if not isinstance(item, dict):
            return None
        lat = _as_float(item.get("lat"))
        lon = _as_float(item.get("lon"))
        if not lat and not lon:
            return None
        return {
            "query": query,
            "name": str(item.get("name") or item.get("display_name") or query),
            "display_name": str(item.get("display_name") or item.get("name") or query),
            "lat": lat,
            "lon": lon,
            "source": "nominatim",
        }

    result, error = _provider_attempt(run)
    if error:
        return None, error
    if result is None:
        return None, "no place match"
    return result, ""


def _distance_km(origin: dict, destination: dict) -> float:
    lat1 = math.radians(float(origin["lat"]))
    lon1 = math.radians(float(origin["lon"]))
    lat2 = math.radians(float(destination["lat"]))
    lon2 = math.radians(float(destination["lon"]))
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return 6371.0 * c


def _round_distance(value: float) -> float:
    if value >= 100:
        return round(value, 1)
    if value >= 10:
        return round(value, 2)
    return round(value, 3)


def _distance_payload(km: float) -> dict:
    return {
        "distance_km": _round_distance(km),
        "distance_miles": _round_distance(km * 0.621371),
    }


def _format_duration(minutes: float | None) -> str:
    if minutes is None:
        return "not available"
    if minutes < 60:
        return f"{round(minutes, 1)} min"
    hours = int(minutes // 60)
    mins = int(round(minutes - (hours * 60)))
    if mins:
        return f"{hours} hr {mins} min"
    return f"{hours} hr"


def _route(origin: dict, destination: dict, mode: str, alternatives: bool, timeout_seconds: float) -> tuple[dict | None, str]:
    coords = f"{origin['lon']},{origin['lat']};{destination['lon']},{destination['lat']}"
    route_url = f"{settings.navigator_route_url.rstrip('/')}/route/v1/{mode}/{coords}"

    def run():
        response = httpx.get(
            route_url,
            params={
                "overview": "full",
                "geometries": "polyline",
                "alternatives": "true" if alternatives else "false",
                "steps": "false",
            },
            timeout=timeout_seconds,
            headers=_headers(),
        )
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            return None
        routes = data.get("routes")
        if not isinstance(routes, list) or not routes:
            return None
        first = routes[0]
        if not isinstance(first, dict):
            return None
        distance_m = _as_float(first.get("distance"), 0.0)
        duration_s = _as_float(first.get("duration"), 0.0)
        if distance_m <= 0:
            return None
        return {
            **_distance_payload(distance_m / 1000),
            "duration_minutes": round(duration_s / 60, 1) if duration_s > 0 else None,
            "duration_text": _format_duration(duration_s / 60 if duration_s > 0 else None),
            "provider": "osrm",
            "status": str(data.get("code") or "ok"),
            "geometry": str(first.get("geometry") or ""),
            "fallback_used": False,
        }

    result, error = _provider_attempt(run)
    if error:
        return None, error
    if result is None:
        return None, "no route returned"
    return result, ""


def _result_summary(result: dict) -> str:
    route = result["route"]
    straight = result["straight_line"]
    origin = result["origin"]
    destination = result["destination"]
    lines = [
        "Navigator result",
        f"From: {origin['display_name']}",
        f"To: {destination['display_name']}",
        f"Mode: {result['mode']}",
        f"Distance: {route['distance_km']} km ({route['distance_miles']} mi)",
        f"Estimated time: {route['duration_text']}",
        f"Straight-line distance: {straight['distance_km']} km ({straight['distance_miles']} mi)",
        f"Providers: {' + '.join(result['provider_sequence'])}",
    ]
    if route.get("fallback_used"):
        lines.append(f"Route status: straight-line fallback because {route.get('fallback_reason')}")
    else:
        lines.append("Route status: routed by OSRM")
    return "\n".join(lines)


@tool(
    name="navigator",
    description="Find route distance, straight-line distance, travel estimate, and place details between two locations using open navigation providers.",
    examples=[
        "show distance from Delhi to Jaipur",
        "how far is Mumbai from Pune by driving",
        "route distance between my city and Haryana",
    ],
    param_descriptions={
        "origin": "Starting place or address",
        "destination": "Destination place or address",
        "mode": "Route mode: driving, walking, or cycling",
        "alternatives": "Whether the routing provider may return alternatives",
        "timeout_ms": "External request timeout in milliseconds, from 1 to 60000",
        "response_format": "legacy or structured. Default legacy preserves existing output",
        "trace_enabled": "When true, emit a machine-readable trace entry",
    },
)
def navigator(
    origin: str,
    destination: str,
    mode: str = "driving",
    alternatives: bool = False,
    timeout_ms: int = 0,
    response_format: str = "legacy",
    trace_enabled: bool = False,
):
    started = time.perf_counter()
    started_at = utc_now_iso()
    inputs_received = 7
    response_format = normalize_response_format(response_format)
    trace_enabled = coerce_bool(trace_enabled)
    alternatives = coerce_bool(alternatives)
    origin = str(origin or "").strip()
    destination = str(destination or "").strip()
    mode = str(mode or settings.navigator_default_mode).strip().lower()

    if not origin:
        error = error_payload(
            "EMPTY_ORIGIN",
            "origin must not be empty.",
            "origin",
            origin,
            "starting place or address",
            False,
            "Pass a starting place in origin.",
        )
        return _navigator_error(error, response_format, trace_enabled, started, started_at, inputs_received, "Navigator needs an origin.")
    if not destination:
        error = error_payload(
            "EMPTY_DESTINATION",
            "destination must not be empty.",
            "destination",
            destination,
            "destination place or address",
            False,
            "Pass a destination place in destination.",
        )
        return _navigator_error(error, response_format, trace_enabled, started, started_at, inputs_received, "Navigator needs a destination.")
    if mode not in _ROUTE_MODES:
        error = error_payload(
            "INVALID_MODE",
            "mode must be driving, walking, or cycling.",
            "mode",
            mode,
            "driving, walking, or cycling",
            False,
            "Use mode='driving' unless the user asks for walking or cycling.",
        )
        return _navigator_error(error, response_format, trace_enabled, started, started_at, inputs_received, "Navigator mode is not supported.")

    timeout_value, timeout_error = normalize_timeout_ms(timeout_ms, settings.navigator_default_timeout_ms)
    if timeout_error is not None:
        return _navigator_error(
            timeout_error,
            response_format,
            trace_enabled,
            started,
            started_at,
            inputs_received,
            "Navigator received an invalid timeout.",
        )
    timeout_seconds = timeout_value / 1000

    origin_place, origin_error = _geocode(origin, timeout_seconds)
    destination_place, destination_error = _geocode(destination, timeout_seconds)
    external_count = 2
    if origin_place is None or destination_place is None:
        missing_field = "origin" if origin_place is None else "destination"
        missing_value = origin if origin_place is None else destination
        detail = origin_error if origin_place is None else destination_error
        error = error_payload(
            "PLACE_NOT_FOUND",
            "One of the requested places could not be resolved by the geocoding provider.",
            missing_field,
            missing_value,
            "place resolved by open geocoding",
            True,
            "Try a more specific city, state, landmark, or full address.",
        )
        error["provider_status"] = detail
        return _navigator_error(
            error,
            response_format,
            trace_enabled,
            started,
            started_at,
            inputs_received,
            f"Navigator could not resolve {missing_field}: {detail}",
            "geocode",
            external_count,
        )

    straight_km = _distance_km(origin_place, destination_place)
    route, route_error = _route(origin_place, destination_place, mode, alternatives, timeout_seconds)
    external_count += 1
    fallback_used = route is None
    if route is None:
        route = {
            **_distance_payload(straight_km),
            "duration_minutes": None,
            "duration_text": "not available",
            "provider": "haversine",
            "status": "fallback",
            "geometry": "",
            "fallback_used": True,
            "fallback_reason": route_error,
        }

    result = {
        "origin_query": origin,
        "destination_query": destination,
        "origin": origin_place,
        "destination": destination_place,
        "mode": mode,
        "alternatives_requested": alternatives,
        "provider_sequence": ["nominatim", route["provider"]],
        "route": route,
        "straight_line": _distance_payload(straight_km),
        "degraded": fallback_used,
        "degraded_reason": route_error if fallback_used else "",
        "narrative": {
            "headline": f"{origin_place['name']} to {destination_place['name']}",
            "summary": _result_summary(
                {
                    "origin": origin_place,
                    "destination": destination_place,
                    "mode": mode,
                    "provider_sequence": ["nominatim", route["provider"]],
                    "route": route,
                    "straight_line": _distance_payload(straight_km),
                }
            ),
            "details": [
                f"Route distance: {route['distance_km']} km",
                f"Straight-line distance: {_distance_payload(straight_km)['distance_km']} km",
                f"Provider status: {'fallback' if fallback_used else 'ok'}",
            ],
        },
    }
    trace = _navigator_trace(
        started_at,
        started,
        inputs_received,
        True,
        "route_fallback" if fallback_used else "route",
        "PARTIAL" if fallback_used else "SUCCESS",
        len(result),
        external_count,
        "ROUTE_FALLBACK" if fallback_used else None,
    )
    emit_trace(trace, trace_enabled)
    if response_format == "structured":
        return structured_success("navigator", _NAVIGATOR_VERSION, result, started, trace)
    return result["narrative"]["summary"]
