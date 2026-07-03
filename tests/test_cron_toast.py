from unittest.mock import MagicMock
from ares.cron.toast import CronToastManager


def test_toast_renders_completed_job():
    console = MagicMock()
    toast = CronToastManager(console)
    toast("stock-monitor", "AAPL up 1.2%", "completed", 3.4)
    console.print.assert_called_once()
    printed = console.print.call_args[0][0]
    # Check that the job name is in the rendered text
    assert "stock-monitor" in printed._text or any(
        span[1] == "stock-monitor" for span in printed._spans
    )


def test_toast_renders_failed_job():
    console = MagicMock()
    toast = CronToastManager(console)
    toast("stock-monitor", "API timeout", "failed", 5.0)
    console.print.assert_called_once()


def test_toast_truncates_long_summary():
    console = MagicMock()
    toast = CronToastManager(console)
    long_summary = "A" * 200
    toast("job", long_summary, "completed", 1.0)
    printed = console.print.call_args[0][0]
    # The full rendered text should not contain the full 200-char summary
    rendered = printed.plain
    assert "A" * 200 not in rendered
    # Should contain truncated summary (60 chars of A's)
    assert "A" * 60 in rendered
