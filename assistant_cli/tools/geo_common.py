from __future__ import annotations

from .core import JsonObject, ToolContext


def geocode_location(ctx: ToolContext, location: str, count: int = 5) -> list[JsonObject]:
    response = ctx.http.get(
        "https://geocoding-api.open-meteo.com/v1/search",
        params={"name": location, "count": count, "language": "en", "format": "json"},
    )
    response.raise_for_status()
    data = response.json()
    rows = data.get("results") if isinstance(data, dict) else []
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def place_label(row: JsonObject) -> str:
    parts = [
        str(row.get("name") or "").strip(),
        str(row.get("admin1") or "").strip(),
        str(row.get("country") or "").strip(),
    ]
    return ", ".join(part for part in parts if part)
