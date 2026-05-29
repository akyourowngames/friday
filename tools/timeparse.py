"""Relative and absolute time parsing for KING tools.

Converts natural durations ("in 5 min", "2 hours", "tomorrow 9am") and ISO
strings into absolute datetimes. No regex: tokens are split on whitespace and
classified by numeric value and a configurable unit vocabulary.

Unit words live in `TIME_UNITS` (a plain table, not a keyword router) so they can
be extended without code changes elsewhere.
"""

from __future__ import annotations

from datetime import datetime, timedelta

# unit word -> seconds. Plain vocabulary, easy to extend.
TIME_UNITS = {
    "sec": 1, "secs": 1, "second": 1, "seconds": 1, "s": 1,
    "min": 60, "mins": 60, "minute": 60, "minutes": 60, "m": 60,
    "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600, "h": 3600,
    "day": 86400, "days": 86400, "d": 86400,
    "week": 604800, "weeks": 604800, "w": 604800,
}


def _digits_to_int(token: str) -> int | None:
    cleaned = token.strip().lower()
    if cleaned.isdigit():
        return int(cleaned)
    # handle attached forms like "5min" or "10m"
    head = []
    for char in cleaned:
        if char.isdigit():
            head.append(char)
        else:
            break
    if head:
        return int("".join(head))
    return None


def _split_attached_unit(token: str) -> str | None:
    """For a token like '5min' return the trailing unit 'min'."""
    cleaned = token.strip().lower()
    idx = 0
    while idx < len(cleaned) and cleaned[idx].isdigit():
        idx += 1
    tail = cleaned[idx:]
    return tail or None


def parse_relative_seconds(text: str) -> int | None:
    """Sum all <number><unit> pairs found in free text.

    Examples: "in 5 min" -> 300; "1 hour 30 minutes" -> 5400; "10m" -> 600.
    Returns None when no number+unit pair is present.
    """
    tokens = str(text or "").lower().replace(",", " ").split()
    total = 0
    found = False
    pending_number: int | None = None
    for token in tokens:
        # attached form: 5min, 10m, 2hrs
        attached_unit = _split_attached_unit(token)
        attached_number = _digits_to_int(token)
        if attached_unit and attached_unit in TIME_UNITS and attached_number is not None:
            total += attached_number * TIME_UNITS[attached_unit]
            found = True
            pending_number = None
            continue
        # standalone number
        if token.isdigit():
            pending_number = int(token)
            continue
        # standalone unit following a number
        if token in TIME_UNITS and pending_number is not None:
            total += pending_number * TIME_UNITS[token]
            found = True
            pending_number = None
            continue
    return total if found else None


def resolve_when(when_text: str, now: datetime | None = None) -> tuple[datetime | None, str]:
    """Resolve a time expression to an absolute datetime.

    Tries: ISO 8601 first, then relative duration. Returns (datetime, mode)
    where mode is "iso", "relative", or "" when unresolved.
    """
    now = now or datetime.now()
    text = str(when_text or "").strip()
    if not text:
        return None, ""
    try:
        return datetime.fromisoformat(text), "iso"
    except ValueError:
        pass
    seconds = parse_relative_seconds(text)
    if seconds is not None and seconds >= 0:
        return now + timedelta(seconds=seconds), "relative"
    return None, ""
