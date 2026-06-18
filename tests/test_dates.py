"""Tests for date parsing helpers."""

from datetime import datetime, timezone

from ares.dates import parse_user_datetime


def test_parse_iso_datetime_preserves_time():
    parsed = parse_user_datetime("2026-06-19T14:00:00+00:00")
    assert datetime.fromisoformat(parsed).astimezone(timezone.utc) == datetime(
        2026, 6, 19, 14, 0, tzinfo=timezone.utc
    )


def test_parse_natural_datetime_returns_iso():
    base = datetime(2026, 6, 18, 12, 0, tzinfo=timezone.utc)
    parsed = parse_user_datetime("tomorrow at 2pm", base=base, timezone_name="UTC")
    assert datetime.fromisoformat(parsed).astimezone(timezone.utc) == datetime(
        2026, 6, 19, 14, 0, tzinfo=timezone.utc
    )
    assert parsed != "tomorrow at 2pm"
