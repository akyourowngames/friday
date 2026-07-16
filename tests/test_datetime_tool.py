"""Tests for the get_current_datetime tool."""
from __future__ import annotations

from datetime import datetime

from ares.tools.datetime_tool import get_current_datetime_result


def test_returns_current_datetime():
    result = get_current_datetime_result()
    assert "datetime" in result
    assert "date" in result
    assert "time" in result
    assert "timezone" in result
    assert "day_of_week" in result
    assert "unix_timestamp" in result


def test_default_timezone_is_local():
    result = get_current_datetime_result()
    assert isinstance(result["timezone"], str)
    assert len(result["timezone"]) > 0


def test_custom_timezone():
    result = get_current_datetime_result(timezone_name="America/New_York")
    assert result["timezone"] == "America/New_York"
    dt = datetime.fromisoformat(result["datetime"])
    assert dt.tzinfo is not None


def test_date_format():
    result = get_current_datetime_result()
    assert len(result["date"]) == 10
    assert result["date"][4] == "-"
    assert result["date"][7] == "-"


def test_time_format():
    result = get_current_datetime_result()
    assert len(result["time"]) == 8
    assert result["time"][2] == ":"
    assert result["time"][5] == ":"


def test_day_of_week_is_valid():
    result = get_current_datetime_result()
    valid_days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    assert result["day_of_week"] in valid_days


def test_unix_timestamp_is_int():
    result = get_current_datetime_result()
    assert isinstance(result["unix_timestamp"], int)
    assert result["unix_timestamp"] > 1577836800
