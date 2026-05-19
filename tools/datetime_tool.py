from tools.registry import tool


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


@tool(
    name="datetime_info",
    description="Get current date and time for any timezone",
    examples=[
        "what time in Tokyo",
        "current time in London",
        "what's the date in New York",
        "time in Asia/Shanghai",
    ],
)
def datetime_info(timezone: str = "local") -> str:
    from datetime import datetime
    import zoneinfo

    try:
        if timezone.strip().lower() in ("local", "", "here"):
            tz = datetime.now().astimezone().tzinfo
            resolved = "local"
        else:
            norm = timezone.strip()
            try:
                tz = zoneinfo.ZoneInfo(norm)
                resolved = norm
            except zoneinfo.ZoneInfoNotFoundError:
                matches = _timezone_matches(norm, zoneinfo)
                if len(matches) == 1:
                    resolved = matches[0]
                    tz = zoneinfo.ZoneInfo(resolved)
                elif matches:
                    shown = ", ".join(matches[:8])
                    more = f" (+{len(matches) - 8} more)" if len(matches) > 8 else ""
                    return f"Ambiguous timezone '{timezone}'. Matches: {shown}{more}"
                else:
                    raise
        now = datetime.now(tz)
        return f"{now.strftime('%A, %B %d, %Y  %I:%M:%S %p  %Z')} ({resolved})"
    except zoneinfo.ZoneInfoNotFoundError:
        return f"Unknown timezone: '{timezone}'. Try a city like 'Asia/Tokyo' or 'America/New_York'."
    except Exception as e:
        return f"Error: {e}"
