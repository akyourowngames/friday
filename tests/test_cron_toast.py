from unittest.mock import MagicMock, patch
from ares.cron.toast import CronToastManager
from ares.cron.runner import CronRunner
from ares.cron.scheduler import CronScheduler


def test_toast_renders_completed_job():
    console = MagicMock()
    toast = CronToastManager(console)
    with patch("ares.cron.toast.Console") as MockConsole:
        mock_instance = MagicMock()
        MockConsole.return_value = mock_instance
        toast("stock-monitor", "AAPL up 1.2%", "completed", 3.4)
        mock_instance.print.assert_called_once()
        printed = mock_instance.print.call_args[0][0]
        # Check that the job name is in the rendered text
        assert "stock-monitor" in printed.plain


def test_toast_renders_failed_job():
    console = MagicMock()
    toast = CronToastManager(console)
    with patch("ares.cron.toast.Console") as MockConsole:
        mock_instance = MagicMock()
        MockConsole.return_value = mock_instance
        toast("stock-monitor", "API timeout", "failed", 5.0)
        mock_instance.print.assert_called_once()


def test_toast_truncates_long_summary():
    console = MagicMock()
    toast = CronToastManager(console)
    long_summary = "A" * 200
    with patch("ares.cron.toast.Console") as MockConsole:
        mock_instance = MagicMock()
        MockConsole.return_value = mock_instance
        toast("job", long_summary, "completed", 1.0)
        printed = mock_instance.print.call_args[0][0]
        # The full rendered text should not contain the full 200-char summary
        rendered = printed.plain
        assert "A" * 200 not in rendered
        # Should contain truncated summary (60 chars of A's)
        assert "A" * 60 in rendered


def test_toast_includes_status_icon():
    console = MagicMock()
    toast = CronToastManager(console)
    with patch("ares.cron.toast.Console") as MockConsole:
        mock_instance = MagicMock()
        MockConsole.return_value = mock_instance
        toast("job", "done", "completed", 1.0)
        printed = mock_instance.print.call_args[0][0]
        assert "✅" in printed.plain

    with patch("ares.cron.toast.Console") as MockConsole:
        mock_instance = MagicMock()
        MockConsole.return_value = mock_instance
        toast("job", "error", "failed", 1.0)
        printed = mock_instance.print.call_args[0][0]
        assert "❌" in printed.plain


def test_runner_stores_on_complete():
    callback = MagicMock()
    store = MagicMock()
    runner = CronRunner(store=store, on_complete=callback)
    assert runner.on_complete is callback


def test_runner_default_on_complete_is_none():
    store = MagicMock()
    runner = CronRunner(store=store)
    assert runner.on_complete is None


def test_scheduler_passes_on_complete():
    callback = MagicMock()
    store = MagicMock()
    scheduler = CronScheduler(store, on_complete=callback)
    assert scheduler.on_complete is callback


def test_scheduler_default_on_complete_is_none():
    store = MagicMock()
    scheduler = CronScheduler(store)
    assert scheduler.on_complete is None
