from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .args import str_arg
from .core import JsonObject, ToolContext, ToolResult, ToolSpec, ok, schema


SPEC = ToolSpec(
    name="current_time",
    description="Show current date and time for a timezone.",
    parameters=schema({"timezone": {"type": "string", "default": "local"}}),
    examples=("current_time timezone=Asia/Kolkata",),
)


def run(ctx: ToolContext, args: JsonObject) -> ToolResult:
    timezone_name = str_arg(args, "timezone", "local")
    if timezone_name.lower() in {"", "local"}:
        now = datetime.now().astimezone()
        label = str(now.tzinfo)
    else:
        try:
            tz = ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"Unknown timezone: {timezone_name}") from exc
        now = datetime.now(tz)
        label = timezone_name
    text = f"{now.strftime('%Y-%m-%d %H:%M:%S %Z')} ({label})"
    return ok("current_time", text, {"timezone": label, "iso": now.isoformat()})
