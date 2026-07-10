"""Cron schedule parsing and validation utilities."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any
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


def simulate_next_runs(
    expr: str,
    tz_name: str = "UTC",
    *,
    base: datetime | None = None,
    count: int = 5,
    last_run_at: str | datetime | None = None,
) -> dict[str, Any]:
    """Simulate upcoming runs and explain missed runs for a cron schedule."""
    validate_cron(expr)
    tz = ZoneInfo(tz_name or "UTC")
    base_utc = base or datetime.now(timezone.utc)
    if base_utc.tzinfo is None:
        base_utc = base_utc.replace(tzinfo=timezone.utc)
    local_base = base_utc.astimezone(tz)
    itr = croniter(expr, local_base)
    bounded = max(1, min(int(count), 20))
    upcoming = []
    for _ in range(bounded):
        local_run = itr.get_next(datetime)
        if local_run.tzinfo is None:
            local_run = local_run.replace(tzinfo=tz)
        utc_run = local_run.astimezone(timezone.utc)
        upcoming.append({
            "local": local_run.isoformat(),
            "utc": utc_run.isoformat().replace("+00:00", "Z"),
        })

    missed = 0
    missed_truncated = False
    explanation = "No previous run timestamp was provided."
    if last_run_at:
        last = _parse_maybe_datetime(last_run_at)
        if last is not None:
            last_local = last.astimezone(tz)
            catchup = croniter(expr, last_local)
            missed_cap = 100
            while missed < missed_cap:
                candidate = catchup.get_next(datetime)
                if candidate.tzinfo is None:
                    candidate = candidate.replace(tzinfo=tz)
                if candidate.astimezone(timezone.utc) > base_utc:
                    break
                missed += 1
            if missed == missed_cap:
                # Do one bounded look-ahead so callers never mistake the cap
                # for an exact count when a machine was offline for a long
                # time.  We intentionally do not enumerate an unbounded
                # backlog here.
                candidate = catchup.get_next(datetime)
                if candidate.tzinfo is None:
                    candidate = candidate.replace(tzinfo=tz)
                missed_truncated = candidate.astimezone(timezone.utc) <= base_utc
            explanation = (
                f"{'At least ' if missed_truncated else ''}{missed} scheduled run(s) were missed between "
                f"{last.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')} and "
                f"{base_utc.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')}."
            )
            if missed_truncated:
                explanation += " The missed-run count is capped at 100."
        else:
            explanation = "Previous run timestamp could not be parsed."

    return {
        "cron": expr,
        "timezone": tz_name or "UTC",
        "base_utc": base_utc.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "next_runs": upcoming,
        "missed_runs": missed,
        "missed_runs_truncated": missed_truncated,
        "missed_runs_lower_bound": missed if missed_truncated else None,
        "missed_run_explanation": explanation,
    }


def _parse_maybe_datetime(value: str | datetime) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed
