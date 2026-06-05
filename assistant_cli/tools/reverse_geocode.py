from __future__ import annotations

from .args import float_arg
from .core import JsonObject, ToolContext, ToolResult, ToolSpec, fail, ok, schema


SPEC = ToolSpec(
    name="reverse_geocode",
    description="Convert latitude/longitude into a human-readable place.",
    parameters=schema(
        {
            "latitude": {"type": "number"},
            "longitude": {"type": "number"},
        },
        required=("latitude", "longitude"),
    ),
    examples=("reverse_geocode latitude=28.65195 longitude=77.23149",),
)


def run(ctx: ToolContext, args: JsonObject) -> ToolResult:
    latitude = float_arg(args, "latitude")
    longitude = float_arg(args, "longitude")
    response = ctx.http.get(
        "https://nominatim.openstreetmap.org/reverse",
        params={"format": "jsonv2", "lat": latitude, "lon": longitude, "zoom": 10},
        headers={"User-Agent": "FridayCLI/1.0"},
    )
    response.raise_for_status()
    data = response.json()
    label = str(data.get("display_name") or "").strip() if isinstance(data, dict) else ""
    if not label:
        return fail("reverse_geocode", "No place found for those coordinates.")
    return ok("reverse_geocode", label, {"latitude": latitude, "longitude": longitude, "place": label})
