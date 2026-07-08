from datetime import datetime, timezone

from ares.cron.schedule_utils import parse_natural_schedule, simulate_next_runs, validate_cron

def test_parse_common_natural_schedules():
    assert parse_natural_schedule('every day at 9am') == '0 9 * * *'
    assert parse_natural_schedule('every weekday at 9:30am') == '30 9 * * 1-5'
    assert parse_natural_schedule('every 5 minutes') == '*/5 * * * *'
    assert parse_natural_schedule('every monday at 10am') == '0 10 * * 1'

def test_validate_cron_rejects_invalid():
    try:
        validate_cron('not cron')
    except ValueError as exc:
        assert 'Invalid cron expression' in str(exc)
    else:
        raise AssertionError('expected invalid cron')


def test_simulate_next_runs_reports_timezone_and_missed_runs():
    report = simulate_next_runs(
        "0 9 * * *",
        "Asia/Kolkata",
        base=datetime(2026, 7, 8, 6, 0, tzinfo=timezone.utc),
        count=2,
        last_run_at="2026-07-06T00:00:00Z",
    )

    assert report["timezone"] == "Asia/Kolkata"
    assert len(report["next_runs"]) == 2
    assert report["missed_runs"] >= 1
    assert "scheduled run" in report["missed_run_explanation"]
