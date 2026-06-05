from __future__ import annotations

from .args import int_arg, str_arg
from .core import JsonObject, ToolContext, ToolResult, ToolSpec, fail, ok, schema
from .geo_common import geocode_location, place_label


SPEC = ToolSpec(
    name="geocode",
    description="Convert a place name into latitude/longitude coordinates.",
    parameters=schema(
        {
            "location": {"type": "string", "description": "City, address, or place name."},
            "count": {"type": "integer", "minimum": 1, "maximum": 10, "default": 5},
        },
        required=("location",),
    ),
    examples=("geocode location=\"New Delhi\"",),
)


def run(ctx: ToolContext, args: JsonObject) -> ToolResult:
    location = str_arg(args, "location")
    if not location:
        return fail("geocode", "location is required")
    count = int_arg(args, "count", 5, 1, 10)
    rows = geocode_location(ctx, location, count)
    if not rows:
        return fail("geocode", f"No coordinates found for {location}.", {"location": location})
    lines = [f"Geocode results for: {location}"]
    for index, row in enumerate(rows, 1):
        name = place_label(row)
        lines.append(f"{index}. {name} | lat={row.get('latitude')} lon={row.get('longitude')}")
    return ok("geocode", "\n".join(lines), {"location": location, "results": rows})
