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


_NAVIGATOR_VERSION = "2.1.0"
_ROUTE_MODES = ("driving", "walking", "cycling")
_NAVIGATOR_ACTIONS = ("route", "geocode", "straight_line")
_BROAD_PLACE_TYPES = ("administrative", "state", "province", "region")


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


def _as_int(value, fallback: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _bounding_box(item: dict) -> list[float]:
    box = item.get("boundingbox")
    if not isinstance(box, list) or len(box) != 4:
        return []
    values = []
    for value in box:
        values.append(_as_float(value))
    return values


def _place_precision(item: dict) -> dict:
    category = str(item.get("category") or item.get("class") or "")
    place_type = str(item.get("type") or "")
    place_rank = _as_int(item.get("place_rank"), 0)
    bounding_box = _bounding_box(item)
    broad = category == "boundary" or place_type in _BROAD_PLACE_TYPES or (place_rank and place_rank < 12)
    scope = "region" if broad else "place"
    return {
        "scope": scope,
        "category": category,
        "type": place_type,
        "place_rank": place_rank,
        "importance": _as_float(item.get("importance"), 0.0),
        "bounding_box": bounding_box,
        "representative_point": broad,
    }


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
            "precision": _place_precision(item),
        }

    result, error = _provider_attempt(run)
    if error:
        return None, error
    if result is None:
        return None, "no place match"
    return result, ""


def _decode_polyline(encoded: str) -> list[dict]:
    points = []
    if not encoded:
        return points
    index = 0
    lat = 0
    lon = 0
    length = len(encoded)
    while index < length:
        result = 0
        shift = 0
        byte = 0
        while index < length:
            byte = ord(encoded[index]) - 63
            index += 1
            result |= (byte & 0x1F) << shift
            shift += 5
            if byte < 0x20:
                break
        lat += ~(result >> 1) if result & 1 else result >> 1

        result = 0
        shift = 0
        while index < length:
            byte = ord(encoded[index]) - 63
            index += 1
            result |= (byte & 0x1F) << shift
            shift += 5
            if byte < 0x20:
                break
        lon += ~(result >> 1) if result & 1 else result >> 1
        points.append({"lat": lat / 100000, "lon": lon / 100000})
    return points


def _route_sample_points(route: dict) -> list[dict]:
    points = _decode_polyline(str(route.get("geometry") or ""))
    if len(points) < 5:
        return []
    sample_count = max(0, min(int(settings.navigator_route_place_samples), 6))
    samples = []
    for step in range(1, sample_count + 1):
        fraction = step / (sample_count + 1)
        index = min(len(points) - 1, max(0, round((len(points) - 1) * fraction)))
        point = dict(points[index])
        point["fraction"] = round(fraction, 3)
        samples.append(point)
    return samples


def _address_place_name(address: dict, fallback: str) -> str:
    if not isinstance(address, dict):
        return fallback
    for key in ("city", "town", "municipality", "village", "county", "state_district", "state"):
        value = address.get(key)
        if value:
            return str(value)
    return fallback


def _reverse_place(point: dict, timeout_seconds: float) -> tuple[dict | None, str]:
    def run():
        response = httpx.get(
            settings.navigator_reverse_url,
            params={
                "lat": point["lat"],
                "lon": point["lon"],
                "format": "jsonv2",
                "addressdetails": 1,
                "zoom": 10,
            },
            timeout=timeout_seconds,
            headers=_headers(),
        )
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            return None
        address = data.get("address") if isinstance(data.get("address"), dict) else {}
        display = str(data.get("display_name") or "")
        name = _address_place_name(address, display)
        if not name:
            return None
        return {
            "name": name,
            "display_name": display or name,
            "lat": point["lat"],
            "lon": point["lon"],
            "fraction": point["fraction"],
            "source": "nominatim_reverse",
        }

    result, error = _provider_attempt(run, attempts=1, delay=0)
    if error:
        return None, error
    if result is None:
        return None, "no reverse place match"
    return result, ""


def _route_places(route: dict, timeout_seconds: float) -> tuple[list[dict], dict, int]:
    places = []
    seen = set()
    errors = []
    external_count = 0
    for point in _route_sample_points(route):
        external_count += 1
        place, error = _reverse_place(point, timeout_seconds)
        if error:
            errors.append(error)
        if not place:
            continue
        key = str(place.get("name") or "").strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        places.append(place)
    status = {
        "provider": "nominatim_reverse",
        "requested_samples": len(_route_sample_points(route)),
        "returned_places": len(places),
        "degraded": bool(errors),
        "errors": errors[:3],
    }
    return places, status, external_count


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
    precision_note = result.get("precision_note")
    if precision_note:
        lines.append(f"Precision note: {precision_note}")
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
        "action": "route (default), geocode, or straight_line",
        "mode": "Route mode: driving, walking, or cycling",
        "alternatives": "Whether the routing provider may return alternatives",
        "timeout_ms": "External request timeout in milliseconds, from 1 to 60000",
        "response_format": "legacy or structured. Default legacy preserves existing output",
        "trace_enabled": "When true, emit a machine-readable trace entry",
    },
)
def navigator(
    origin: str,
    destination: str = "",
    action: str = "route",
    mode: str = "driving",
    alternatives: bool = False,
    timeout_ms: int = 0,
    response_format: str = "legacy",
    trace_enabled: bool = False,
):
    started = time.perf_counter()
    started_at = utc_now_iso()
    inputs_received = 8
    response_format = normalize_response_format(response_format)
    trace_enabled = coerce_bool(trace_enabled)
    alternatives = coerce_bool(alternatives)
    origin = str(origin or "").strip()
    destination = str(destination or "").strip()
    mode = str(mode or settings.navigator_default_mode).strip().lower()
    action = str(action or "route").strip().lower()

    if action not in _NAVIGATOR_ACTIONS:
        error = error_payload(
            "INVALID_ACTION",
            "action must be route, geocode, or straight_line.",
            "action",
            action,
            "route, geocode, or straight_line",
            False,
            "Use action='route' for full navigation or action='geocode' for place lookup only.",
        )
        return _navigator_error(error, response_format, trace_enabled, started, started_at, inputs_received, "Navigator action is not supported.")

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
    if action != "geocode" and not destination:
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
    if action == "route" and mode not in _ROUTE_MODES:
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
    external_count = 1
    if action == "geocode":
        if origin_place is None:
            error = error_payload(
                "PLACE_NOT_FOUND",
                "The place could not be resolved by the geocoding provider.",
                "origin",
                origin,
                "resolved place",
                True,
                origin_error,
            )
            return _navigator_error(error, response_format, trace_enabled, started, started_at, inputs_received, f"Navigator could not resolve place: {origin_error}", "geocode", external_count)
        result = {
            "action": "geocode",
            "query": origin,
            "place": origin_place,
            "provider_sequence": ["nominatim"],
            "degraded": False,
            "degraded_reason": "",
        }
        trace = _navigator_trace(started_at, started, inputs_received, True, "geocode", "SUCCESS", len(result), external_count)
        emit_trace(trace, trace_enabled)
        if response_format == "structured":
            return structured_success("navigator", _NAVIGATOR_VERSION, result, started, trace)
        return f"{origin_place['name']} ({origin_place['lat']}, {origin_place['lon']})"

    destination_place, destination_error = _geocode(destination, timeout_seconds)
    external_count = 2
    if action == "straight_line":
        if origin_place is None or destination_place is None:
            missing_field = "origin" if origin_place is None else "destination"
            detail = origin_error if origin_place is None else destination_error
            error = error_payload(
                "PLACE_NOT_FOUND",
                "One of the places could not be resolved.",
                missing_field,
                origin if origin_place is None else destination,
                "resolved place",
                True,
                detail,
            )
            return _navigator_error(error, response_format, trace_enabled, started, started_at, inputs_received, f"Navigator could not resolve {missing_field}", "geocode", external_count)
        straight_km = _distance_km(origin_place, destination_place)
        result = {
            "action": "straight_line",
            "origin_query": origin,
            "destination_query": destination,
            "origin": origin_place,
            "destination": destination_place,
            "straight_line": _distance_payload(straight_km),
            "provider_sequence": ["nominatim"],
            "degraded": False,
            "degraded_reason": "",
        }
        trace = _navigator_trace(started_at, started, inputs_received, True, "straight_line", "SUCCESS", len(result), external_count)
        emit_trace(trace, trace_enabled)
        if response_format == "structured":
            return structured_success("navigator", _NAVIGATOR_VERSION, result, started, trace)
        payload = _distance_payload(straight_km)
        return f"Straight-line distance: {payload['distance_km']} km ({payload['distance_miles']} mi)"

    if action != "route":
        error = error_payload("INVALID_ACTION", "Unsupported navigator action.", "action", action, "route, geocode, straight_line", False, "Use a supported action.")
        return _navigator_error(error, response_format, trace_enabled, started, started_at, inputs_received, "Navigator action is not supported.")
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
    route_places = []
    route_place_status = {"provider": "nominatim_reverse", "requested_samples": 0, "returned_places": 0, "degraded": False, "errors": []}
    if not route.get("fallback_used"):
        route_places, route_place_status, reverse_count = _route_places(route, timeout_seconds)
        external_count += reverse_count

    precision_notes = []
    if origin_place.get("precision", {}).get("representative_point"):
        precision_notes.append(f"{origin_place['name']} is a region; the route starts from its representative coordinate")
    if destination_place.get("precision", {}).get("representative_point"):
        precision_notes.append(f"{destination_place['name']} is a region; the route ends at its representative coordinate")
    precision_note = "; ".join(precision_notes)
    degraded = fallback_used or bool(precision_note) or bool(route_place_status.get("degraded"))
    degraded_reason = route_error if fallback_used else precision_note

    result = {
        "action": "route",
        "origin_query": origin,
        "destination_query": destination,
        "origin": origin_place,
        "destination": destination_place,
        "mode": mode,
        "alternatives_requested": alternatives,
        "provider_sequence": ["nominatim", route["provider"]],
        "route": route,
        "route_places": route_places,
        "route_place_status": route_place_status,
        "straight_line": _distance_payload(straight_km),
        "degraded": degraded,
        "degraded_reason": degraded_reason,
        "precision_note": precision_note,
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
                    "precision_note": precision_note,
                }
            ),
            "details": [
                f"Route distance: {route['distance_km']} km",
                f"Straight-line distance: {_distance_payload(straight_km)['distance_km']} km",
                f"Route places: {', '.join([place['name'] for place in route_places]) if route_places else 'not available'}",
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
        "PARTIAL" if degraded else "SUCCESS",
        len(result),
        external_count,
        "ROUTE_FALLBACK" if fallback_used else ("REPRESENTATIVE_POINT" if precision_note else None),
    )
    emit_trace(trace, trace_enabled)
    if response_format == "structured":
        return structured_success("navigator", _NAVIGATOR_VERSION, result, started, trace)
    return result["narrative"]["summary"]
