"""Date/time helpers for user-facing task dates."""

from __future__ import annotations

from datetime import datetime


def local_timezone_name() -> str:
    """Return the local IANA timezone name when available."""
    try:
        from tzlocal import get_localzone_name

        return get_localzone_name()
    except Exception:
        return datetime.now().astimezone().tzinfo.tzname(None) or "UTC"


def now_local() -> datetime:
    """Return the current local time as a timezone-aware datetime."""
    return datetime.now().astimezone()


def now_local_iso() -> str:
    """Return the current local time in stable ISO format."""
    return now_local().isoformat(timespec="seconds")


def _parse_iso(value: str) -> datetime | None:
    normalized = value.strip()
    if not normalized:
        return None
    if normalized.endswith("Z"):
        normalized = f"{normalized[:-1]}+00:00"
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        return None


def parse_user_datetime(
    value: str | None,
    *,
    base: datetime | None = None,
    timezone_name: str | None = None,
) -> str | None:
    """Normalize ISO or natural-language dates to timezone-aware ISO strings."""
    if value is None:
        return None

    text = value.strip()
    if not text:
        return None

    tz_name = timezone_name or local_timezone_name()
    parsed = _parse_iso(text)

    if parsed is None:
        try:
            import dateparser

            parsed = dateparser.parse(
                text,
                settings={
                    "PREFER_DATES_FROM": "future",
                    "RELATIVE_BASE": base or now_local(),
                    "TIMEZONE": tz_name,
                    "TO_TIMEZONE": tz_name,
                    "RETURN_AS_TIMEZONE_AWARE": True,
                },
            )
        except Exception:
            parsed = None

    if parsed is None:
        return text

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=now_local().tzinfo)

    return parsed.astimezone(now_local().tzinfo).isoformat(timespec="seconds")
