"""Get current datetime as a tool for the agent."""
from __future__ import annotations

import calendar
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ares.tools.dates import local_timezone_name


def get_current_datetime_result(timezone_name: str | None = None) -> dict:
    """Return current date/time as a structured dict.

    Args:
        timezone_name: Optional IANA timezone (e.g. 'America/New_York').
                       Defaults to the system's local timezone.
    """
    requested_timezone = bool(timezone_name)
    tz_name = timezone_name or local_timezone_name()

    try:
        tz = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError):
        if not requested_timezone:
            tz = datetime.now().astimezone().tzinfo or timezone.utc
            tz_name = str(tz_name or "local")
        else:
            return {
                "ok": False,
                "error": f"Invalid IANA timezone: {tz_name}",
                "timezone": tz_name,
            }

    now = datetime.now(tz)

    return {
        "ok": True,
        "datetime": now.isoformat(timespec="seconds"),
        "date": now.strftime("%Y-%m-%d"),
        "time": now.strftime("%H:%M:%S"),
        "timezone": tz_name,
        "day_of_week": calendar.day_name[now.weekday()],
        "unix_timestamp": int(now.timestamp()),
    }
