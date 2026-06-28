"""Cron schedule parsing and validation utilities."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from croniter import croniter


def validate_cron(expr: str) -> str:
    expr = (expr or "").strip()
    if not croniter.is_valid(expr):
        raise ValueError(f"Invalid cron expression: {expr}")
    return expr


def parse_natural_schedule(schedule: str) -> str:
    text = (schedule or "").strip().lower()
    if croniter.is_valid(text):
        return text
    if text in {"hourly", "every hour"}:
        return "0 * * * *"
    m = re.fullmatch(r"every (\d+) minutes?", text)
    if m:
        n = int(m.group(1))
        if n < 1 or n > 59:
            raise ValueError("Minute interval must be between 1 and 59")
        return f"*/{n} * * * *"
    m = re.fullmatch(r"every day at (\d{1,2})(?::(\d{2}))?\s*(am|pm)?", text)
    if m:
        hour, minute = _parse_time(m)
        return f"{minute} {hour} * * *"
    m = re.fullmatch(r"every weekday at (\d{1,2})(?::(\d{2}))?\s*(am|pm)?", text)
    if m:
        hour, minute = _parse_time(m)
        return f"{minute} {hour} * * 1-5"
    days = {"sunday":0,"monday":1,"tuesday":2,"wednesday":3,"thursday":4,"friday":5,"saturday":6}
    m = re.fullmatch(r"every (sunday|monday|tuesday|wednesday|thursday|friday|saturday) at (\d{1,2})(?::(\d{2}))?\s*(am|pm)?", text)
    if m:
        hour, minute = _parse_time(m, offset=2)
        return f"{minute} {hour} * * {days[m.group(1)]}"
    m = re.fullmatch(r"every 1st of the month(?: at (\d{1,2})(?::(\d{2}))?\s*(am|pm)?)?", text)
    if m:
        hour, minute = (9, 0) if m.group(1) is None else _parse_time(m)
        return f"{minute} {hour} 1 * *"
    raise ValueError("Ambiguous schedule; provide a five-field cron expression or a supported natural phrase.")


def _parse_time(match: re.Match, offset: int = 1) -> tuple[int, int]:
    hour = int(match.group(offset)); minute = int(match.group(offset+1) or 0); ampm = match.group(offset+2)
    if minute > 59: raise ValueError("Minute must be 0-59")
    if ampm:
        if not 1 <= hour <= 12: raise ValueError("12-hour time must use 1-12")
        if ampm == "pm" and hour != 12: hour += 12
        if ampm == "am" and hour == 12: hour = 0
    elif not 0 <= hour <= 23:
        raise ValueError("Hour must be 0-23")
    return hour, minute


def next_run_utc(expr: str, tz_name: str = "UTC", base: datetime | None = None) -> str:
    validate_cron(expr)
    tz = ZoneInfo(tz_name or "UTC")
    base = base or datetime.now(timezone.utc)
    local_base = base.astimezone(tz)
    nxt = croniter(expr, local_base).get_next(datetime)
    if nxt.tzinfo is None:
        nxt = nxt.replace(tzinfo=tz)
    return nxt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
