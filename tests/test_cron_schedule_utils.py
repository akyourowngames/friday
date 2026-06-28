from ares.cron.schedule_utils import parse_natural_schedule, validate_cron

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
