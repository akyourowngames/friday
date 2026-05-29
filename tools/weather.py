"""Weather tool via Open-Meteo (no API key required).

Geocodes a place name through the same open Nominatim service the navigator
tool already uses, then fetches current conditions and a short forecast from
Open-Meteo. Network-dependent; returns a structured error when offline rather
than guessing.
"""

import time

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

_WEATHER_VERSION = "1.0.0"
_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

# Open-Meteo WMO weather codes -> short description (plain table, not routing).
_WMO = {
    0: "clear sky", 1: "mainly clear", 2: "partly cloudy", 3: "overcast",
    45: "fog", 48: "depositing rime fog", 51: "light drizzle", 53: "drizzle",
    55: "dense drizzle", 61: "light rain", 63: "rain", 65: "heavy rain",
    71: "light snow", 73: "snow", 75: "heavy snow", 80: "rain showers",
    81: "rain showers", 82: "violent rain showers", 95: "thunderstorm",
    96: "thunderstorm with hail", 99: "thunderstorm with heavy hail",
}


def _trace(name, started_at, started, schema_valid, status, output_fields, error_code=None):
    return make_trace(
        name, _WEATHER_VERSION, started_at, started, 1, schema_valid,
        "weather", status, output_fields, {"count": 2, "systems": ["nominatim", "open_meteo"]}, error_code,
    )


def _emit(name, started, started_at, trace_enabled, result=None, error=None, response_format="legacy", legacy="", status="SUCCESS"):
    valid = error is None
    trace = _trace(name, started_at, started, valid, status if valid else "FAILED",
                   len(result) if result else 1, None if valid else error["code"])
    emit_trace(trace, trace_enabled)
    if response_format == "structured":
        if valid:
            return structured_success(name, _WEATHER_VERSION, result, started, trace)
        return structured_error(name, _WEATHER_VERSION, error, started, trace)
    return legacy


def _geocode(place: str):
    import requests

    params = {"q": place, "format": "json", "limit": 1}
    headers = {"User-Agent": settings.navigator_user_agent}
    resp = requests.get(settings.navigator_geocode_url, params=params, headers=headers, timeout=12)
    resp.raise_for_status()
    data = resp.json()
    if not data:
        return None
    first = data[0]
    return {"lat": float(first["lat"]), "lon": float(first["lon"]), "label": first.get("display_name", place)}


@tool(
    name="weather",
    description="Get current weather and a short forecast for a place using free open data (no API key).",
    examples=[
        "what's the weather in Haryana",
        "weather in Bangalore today",
        "is it going to rain in Delhi",
    ],
    param_descriptions={
        "location": "City or place name",
        "response_format": "legacy or structured",
        "trace_enabled": "When true, emit a machine-readable trace entry",
    },
)
def weather(location: str, response_format: str = "legacy", trace_enabled: bool = False):
    started = time.perf_counter()
    started_at = utc_now_iso()
    response_format = normalize_response_format(response_format)
    trace_enabled = coerce_bool(trace_enabled)

    place = str(location or "").strip()
    if not place:
        err = error_payload("EMPTY_LOCATION", "location must not be empty.", "location", location, "place name", False, "Provide a city or place name.")
        return _emit("weather", started, started_at, trace_enabled, error=err, response_format=response_format, legacy="Error: location is required", status="FAILED")

    try:
        import requests

        geo = _geocode(place)
        if geo is None:
            err = error_payload("PLACE_NOT_FOUND", "Could not locate that place.", "location", place, "resolvable place name", False, "Try a more specific location.")
            return _emit("weather", started, started_at, trace_enabled, error=err, response_format=response_format, legacy=f"Error: could not find '{place}'", status="FAILED")

        params = {
            "latitude": geo["lat"],
            "longitude": geo["lon"],
            "current": "temperature_2m,relative_humidity_2m,apparent_temperature,weather_code,wind_speed_10m",
            "daily": "temperature_2m_max,temperature_2m_min,weather_code",
            "forecast_days": 2,
            "timezone": "auto",
        }
        resp = requests.get(_FORECAST_URL, params=params, timeout=12)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        err = error_payload("WEATHER_FETCH_FAILED", f"{type(exc).__name__}", "weather", None, "reachable weather service", True, "Retry when network is available.")
        return _emit("weather", started, started_at, trace_enabled, error=err, response_format=response_format, legacy="Error: weather lookup failed (offline?)", status="FAILED")

    current = data.get("current", {})
    daily = data.get("daily", {})
    code = int(current.get("weather_code", -1))
    condition = _WMO.get(code, "unknown")
    temp = current.get("temperature_2m")
    feels = current.get("apparent_temperature")
    humidity = current.get("relative_humidity_2m")
    wind = current.get("wind_speed_10m")

    today_max = (daily.get("temperature_2m_max") or [None])[0]
    today_min = (daily.get("temperature_2m_min") or [None])[0]

    result = {
        "location": geo["label"],
        "condition": condition,
        "temperature_c": temp,
        "feels_like_c": feels,
        "humidity_percent": humidity,
        "wind_kmh": wind,
        "today_high_c": today_max,
        "today_low_c": today_min,
    }
    legacy = (
        f"{geo['label'].split(',')[0]}: {condition}, {temp}\u00b0C (feels {feels}\u00b0C), "
        f"humidity {humidity}%, wind {wind} km/h. Today {today_min}\u2013{today_max}\u00b0C."
    )
    return _emit("weather", started, started_at, trace_enabled, result=result, response_format=response_format, legacy=legacy)
