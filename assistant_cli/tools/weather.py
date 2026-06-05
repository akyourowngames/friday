from __future__ import annotations

from .args import float_arg, str_arg
from .core import JsonObject, ToolContext, ToolResult, ToolSpec, fail, ok, schema
from .geo_common import geocode_location, place_label


SPEC = ToolSpec(
    name="weather",
    description="Get current weather by location or latitude/longitude using Open-Meteo.",
    parameters=schema(
        {
            "location": {"type": "string", "description": "City or place name."},
            "latitude": {"type": "number"},
            "longitude": {"type": "number"},
        }
    ),
    examples=("weather location=Delhi",),
)


def run(ctx: ToolContext, args: JsonObject) -> ToolResult:
    location = str_arg(args, "location")
    if "latitude" in args and "longitude" in args:
        latitude = float_arg(args, "latitude")
        longitude = float_arg(args, "longitude")
        place = location or f"{latitude},{longitude}"
    else:
        if not location:
            return fail("weather", "Provide location or latitude and longitude.")
        matches = geocode_location(ctx, location, 1)
        if not matches:
            return fail("weather", f"No coordinates found for {location}.", {"location": location})
        first = matches[0]
        latitude = float(first["latitude"])
        longitude = float(first["longitude"])
        place = place_label(first)

    response = ctx.http.get(
        "https://api.open-meteo.com/v1/forecast",
        params={
            "latitude": latitude,
            "longitude": longitude,
            "current": "temperature_2m,relative_humidity_2m,apparent_temperature,precipitation,weather_code,wind_speed_10m",
            "timezone": "auto",
        },
    )
    response.raise_for_status()
    data = response.json()
    current = data.get("current") if isinstance(data, dict) else {}
    units = data.get("current_units") if isinstance(data, dict) else {}
    if not isinstance(current, dict):
        current = {}
    if not isinstance(units, dict):
        units = {}

    lines = [
        f"Weather for {place}",
        f"Temperature: {current.get('temperature_2m')} {_unit(units.get('temperature_2m', ''))}".strip(),
        f"Feels like: {current.get('apparent_temperature')} {_unit(units.get('apparent_temperature', ''))}".strip(),
        f"Humidity: {current.get('relative_humidity_2m')} {_unit(units.get('relative_humidity_2m', ''))}".strip(),
        f"Wind: {current.get('wind_speed_10m')} {_unit(units.get('wind_speed_10m', ''))}".strip(),
        f"Precipitation: {current.get('precipitation')} {_unit(units.get('precipitation', ''))}".strip(),
    ]
    return ok("weather", "\n".join(lines), {"place": place, "latitude": latitude, "longitude": longitude, "current": current})


def _unit(value: object) -> str:
    return str(value or "").replace(chr(176), "deg")
