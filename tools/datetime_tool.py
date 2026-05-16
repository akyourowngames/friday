from tools.registry import tool


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
        else:
            norm = timezone.strip()
            tz = zoneinfo.ZoneInfo(norm)
        now = datetime.now(tz)
        return now.strftime("%A, %B %d, %Y  %I:%M:%S %p  %Z")
    except zoneinfo.ZoneInfoNotFoundError:
        return f"Unknown timezone: '{timezone}'. Try a city like 'Asia/Tokyo' or 'America/New_York'."
    except Exception as e:
        return f"Error: {e}"
