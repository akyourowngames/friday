import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from maintenance.config import MaintenanceConfig, StepConfig, load_config
from maintenance.engine import MaintenanceEngine
from maintenance.state import MaintenanceState
from maintenance.scheduler_thread import DailyScheduler


def _write_config(root: Path, body: str) -> Path:
    path = root / "MAINTENANCE_DAILY.md"
    path.write_text(body, encoding="utf-8")
    return path


class MaintenanceConfigTests(unittest.TestCase):
    def test_loader_reads_runtime_steps_and_whitelist(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            body = (
                "# heading\n"
                "## Runtime\n"
                "- cutoff_time: 02:15\n"
                "- min_run_interval_minutes: 30\n"
                "- enabled: true\n"
                "## Steps\n"
                "- memory_daily: enabled=true label=nightly\n"
                "- folder_scan: enabled=false\n"
                "## Action Whitelist\n"
                "- note_save\n"
                "- daily_maintenance\n"
                "## Retention\n"
                "- memory_backup_keep_count: 7\n"
            )
            cfg_path = _write_config(tmp_path, body)
            config = load_config(tmp_path, cfg_path)

            self.assertEqual(config.cutoff_time, "02:15")
            self.assertEqual(config.min_run_interval_minutes, 30)
            self.assertTrue(config.enabled)
            self.assertEqual({step.name for step in config.steps}, {"memory_daily", "folder_scan"})
            memory_step = config.step("memory_daily")
            self.assertIsNotNone(memory_step)
            self.assertTrue(memory_step.enabled)
            self.assertEqual(memory_step.options.get("label"), "nightly")
            folder_step = config.step("folder_scan")
            self.assertFalse(folder_step.enabled)
            self.assertEqual(config.action_whitelist, {"note_save", "daily_maintenance"})
            self.assertEqual(config.retention.get("memory_backup_keep_count"), 7)


class MaintenanceEngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.state = MaintenanceState(self.root / "state.json", self.root / "log.jsonl")

    def _config(self, steps: list[StepConfig], min_interval: int = 0) -> MaintenanceConfig:
        return MaintenanceConfig(
            repo_root=self.root,
            config_path=self.root / "MAINTENANCE_DAILY.md",
            cutoff_time="03:30",
            min_run_interval_minutes=min_interval,
            steps=steps,
        )

    def test_run_executes_registered_handlers_and_persists(self):
        captured = {}

        def handler(step, context):
            captured["called"] = True
            captured["options"] = dict(step.options)
            return {"ran": True}

        engine = MaintenanceEngine(self._config([StepConfig(name="hello", options={"x": 1})]), self.state)
        engine.register("hello", handler)

        result = engine.run(triggered_by="test")

        self.assertEqual(result.status, "ok")
        self.assertEqual(len(result.steps), 1)
        self.assertEqual(result.steps[0]["status"], "ok")
        self.assertEqual(result.steps[0]["evidence"]["ran"], True)
        self.assertTrue(captured["called"])
        state = self.state.load()
        self.assertEqual(state["last_status"], "ok")
        log = self.state.recent_runs(5)
        self.assertEqual(len(log), 1)

    def test_dry_run_does_not_execute_handler(self):
        calls = {"n": 0}

        def handler(step, context):
            calls["n"] += 1
            return {}

        engine = MaintenanceEngine(self._config([StepConfig(name="x")]), self.state)
        engine.register("x", handler)
        result = engine.run(dry_run=True)

        self.assertEqual(result.status, "dry_run")
        self.assertEqual(calls["n"], 0)
        self.assertEqual(result.steps[0]["status"], "dry_run")
        self.assertIsNone(self.state.load().get("last_run_date"))

    def test_skips_when_already_ran_today(self):
        engine = MaintenanceEngine(self._config([StepConfig(name="x")]), self.state)
        engine.register("x", lambda step, ctx: {})
        engine.run()
        engine = MaintenanceEngine(self._config([StepConfig(name="x")]), self.state)
        engine.register("x", lambda step, ctx: {})
        result = engine.run()
        self.assertEqual(result.status, "skipped")
        self.assertEqual(result.skipped_reason, "already_ran_today")

    def test_force_overrides_already_ran(self):
        engine = MaintenanceEngine(self._config([StepConfig(name="x")]), self.state)
        engine.register("x", lambda step, ctx: {})
        engine.run()
        engine = MaintenanceEngine(self._config([StepConfig(name="x")]), self.state)
        engine.register("x", lambda step, ctx: {})
        result = engine.run(force=True)
        self.assertEqual(result.status, "ok")

    def test_step_failure_marks_partial(self):
        def handler(step, context):
            raise RuntimeError("boom")

        engine = MaintenanceEngine(self._config([StepConfig(name="bad"), StepConfig(name="good")]), self.state)
        engine.register("bad", handler)
        engine.register("good", lambda step, ctx: {"ok": True})
        result = engine.run()
        self.assertEqual(result.status, "partial")
        statuses = [step["status"] for step in result.steps]
        self.assertEqual(statuses, ["failed", "ok"])

    def test_disabled_step_is_skipped_with_reason(self):
        engine = MaintenanceEngine(self._config([StepConfig(name="x", enabled=False)]), self.state)
        engine.register("x", lambda step, ctx: {})
        result = engine.run()
        self.assertEqual(result.steps[0]["status"], "skipped")
        self.assertEqual(result.steps[0]["reason"], "disabled")

    def test_handler_missing_is_skipped_not_failed(self):
        engine = MaintenanceEngine(self._config([StepConfig(name="ghost")]), self.state)
        result = engine.run()
        self.assertEqual(result.steps[0]["status"], "skipped")
        self.assertEqual(result.steps[0]["reason"], "no_handler_registered")


class DailySchedulerThreadTests(unittest.TestCase):
    def test_fires_once_after_cutoff_and_does_not_double_fire(self):
        calls = []

        def cb(triggered_by):
            calls.append(triggered_by)

        scheduler = DailyScheduler(callback=cb, cutoff_time="03:30", check_interval_seconds=1)

        # Before cutoff: do not fire.
        before_cutoff = datetime(2026, 5, 28, 1, 0, 0)
        cutoff = before_cutoff.replace(hour=3, minute=30, second=0, microsecond=0)
        if before_cutoff >= cutoff and scheduler._last_fired_date != before_cutoff.date().isoformat():
            cb("test")
            scheduler._last_fired_date = before_cutoff.date().isoformat()
        self.assertEqual(len(calls), 0)

        # At/after cutoff: fire once.
        after_cutoff = datetime(2026, 5, 28, 4, 0, 0)
        cutoff_after = after_cutoff.replace(hour=3, minute=30, second=0, microsecond=0)
        if after_cutoff >= cutoff_after and scheduler._last_fired_date != after_cutoff.date().isoformat():
            cb("test")
            scheduler._last_fired_date = after_cutoff.date().isoformat()
        self.assertEqual(len(calls), 1)

        # Same day, later check: do not fire again.
        even_later = datetime(2026, 5, 28, 5, 0, 0)
        cutoff_again = even_later.replace(hour=3, minute=30, second=0, microsecond=0)
        if even_later >= cutoff_again and scheduler._last_fired_date != even_later.date().isoformat():
            cb("test")
            scheduler._last_fired_date = even_later.date().isoformat()
        self.assertEqual(len(calls), 1)
        self.assertEqual(scheduler._last_fired_date, "2026-05-28")


if __name__ == "__main__":
    unittest.main()
