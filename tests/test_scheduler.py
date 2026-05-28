import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scheduler.config import SchedulerConfig, load_config
from scheduler.engine import Scheduler
from scheduler.store import SchedulerStore


def _config_with_whitelist(root: Path, whitelist: set[str]) -> SchedulerConfig:
    return SchedulerConfig(
        repo_root=root,
        config_path=root / "SCHEDULER_CONFIG.md",
        action_whitelist=set(whitelist),
        memory_linkage={},
        notes_linkage={},
        folder_linkage={},
        reschedule_policy={"failed_item_max_retries": 1, "failed_item_retry_minutes": 0},
    )


def _stub_writers():
    note_calls: list[dict] = []
    memory_calls: list[dict] = []

    def note_writer(title, content, tags):
        note_calls.append({"title": title, "content": content, "tags": tags})
        return {"ok": True}

    def memory_writer(text, importance):
        memory_calls.append({"text": text, "importance": importance})
        return {"id": f"mem-{len(memory_calls)}", "stored": True}

    return note_writer, memory_writer, note_calls, memory_calls


class SchedulerConfigLoaderTests(unittest.TestCase):
    def test_loads_runtime_whitelist_and_linkage(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "SCHEDULER_CONFIG.md").write_text(
                "# title\n"
                "## Runtime\n"
                "- check_interval_seconds: 5\n"
                "- max_items_per_run: 7\n"
                "## Action Whitelist\n"
                "- note_save\n"
                "## Memory Linkage\n"
                "- remember_on_create: true\n"
                "- importance_default: 0.5\n"
                "## Notes Linkage\n"
                "- note_on_complete: true\n"
                "- note_title_prefix: \"Sched: \"\n"
                "## Reschedule Policy\n"
                "- failed_item_retry_minutes: 5\n"
                "- failed_item_max_retries: 2\n",
                encoding="utf-8",
            )
            config = load_config(root, root / "SCHEDULER_CONFIG.md")
            self.assertEqual(config.check_interval_seconds, 5)
            self.assertEqual(config.max_items_per_run, 7)
            self.assertEqual(config.action_whitelist, {"note_save"})
            self.assertTrue(config.memory_linkage.get("remember_on_create"))
            self.assertEqual(config.memory_linkage.get("importance_default"), 0.5)
            self.assertEqual(config.notes_linkage.get("note_title_prefix"), "Sched:")
            self.assertEqual(config.reschedule_policy.get("failed_item_max_retries"), 2)


class SchedulerEngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = SchedulerStore(self.root / "store.json", self.root / "log.jsonl")

    def _scheduler(self, whitelist, runner=None, note_writer=None, memory_writer=None, clock=None):
        config = _config_with_whitelist(self.root, whitelist)
        return Scheduler(
            config,
            store=self.store,
            action_runner=runner or (lambda action, args: {"ran": action}),
            note_writer=note_writer or (lambda *args, **kwargs: {"ok": True}),
            memory_writer=memory_writer or (lambda *args, **kwargs: {"id": "memX"}),
            clock=clock or datetime.now,
        )

    def test_schedule_rejects_non_whitelisted_action(self):
        scheduler = self._scheduler({"note_save"})
        with self.assertRaises(ValueError):
            scheduler.schedule("t", "rm -rf /", "2026-05-29T00:00:00")

    def test_schedule_persists_item_with_id_and_status_pending(self):
        scheduler = self._scheduler({"note_save"})
        record = scheduler.schedule("buy milk", "note_save", "2026-05-29T08:00:00", arguments={"title": "milk", "content": "2L"})
        self.assertEqual(record["status"], "pending")
        self.assertEqual(record["action"], "note_save")
        self.assertGreaterEqual(record["id"], 1)
        items = self.store.all_items()
        self.assertEqual(len(items), 1)

    def test_schedule_invalid_iso_rejected(self):
        scheduler = self._scheduler({"note_save"})
        with self.assertRaises(ValueError):
            scheduler.schedule("t", "note_save", "not a date")

    def test_run_due_executes_action_and_marks_completed(self):
        captured = []

        def runner(action, args):
            captured.append((action, args))
            return {"ok": True}

        clock_now = datetime(2026, 5, 28, 12, 0, 0)
        scheduler = self._scheduler({"note_save"}, runner=runner, clock=lambda: clock_now)
        scheduler.schedule("note", "note_save", "2026-05-28T11:00:00", arguments={"title": "x", "content": "y"})

        result = scheduler.run_due(now=clock_now)
        self.assertEqual(result["ran_count"], 1)
        self.assertEqual(result["ran"][0]["status"], "completed")
        self.assertEqual(captured[0][0], "note_save")
        items = self.store.all_items()
        self.assertEqual(items[0]["status"], "completed")

    def test_run_due_skips_future_items(self):
        clock_now = datetime(2026, 5, 28, 12, 0, 0)
        scheduler = self._scheduler({"note_save"}, clock=lambda: clock_now)
        scheduler.schedule("future", "note_save", "2026-05-29T11:00:00", arguments={"title": "x", "content": "y"})
        result = scheduler.run_due(now=clock_now)
        self.assertEqual(result["ran_count"], 0)

    def test_run_due_horizon_includes_near_future(self):
        clock_now = datetime(2026, 5, 28, 12, 0, 0)
        scheduler = self._scheduler({"note_save"}, clock=lambda: clock_now)
        scheduler.schedule("soon", "note_save", "2026-05-28T12:30:00", arguments={"title": "x", "content": "y"})
        result = scheduler.run_due(horizon_minutes=60, now=clock_now)
        self.assertEqual(result["ran_count"], 1)

    def test_failed_action_retries_when_policy_allows(self):
        attempts = {"n": 0}

        def runner(action, args):
            attempts["n"] += 1
            raise RuntimeError("nope")

        clock_now = datetime(2026, 5, 28, 12, 0, 0)
        config = _config_with_whitelist(self.root, {"note_save"})
        config.reschedule_policy = {"failed_item_max_retries": 1, "failed_item_retry_minutes": 5}
        scheduler = Scheduler(config, store=self.store, action_runner=runner, clock=lambda: clock_now)
        scheduler.schedule("flaky", "note_save", "2026-05-28T11:00:00", arguments={})
        result = scheduler.run_due(now=clock_now)
        self.assertEqual(result["ran"][0]["status"], "retry_scheduled")
        items = self.store.all_items()
        self.assertEqual(items[0]["status"], "pending")
        self.assertEqual(items[0]["retry_count"], 1)

    def test_failed_action_marked_failed_after_retries(self):
        def runner(action, args):
            raise RuntimeError("nope")

        clock_now = datetime(2026, 5, 28, 12, 0, 0)
        config = _config_with_whitelist(self.root, {"note_save"})
        config.reschedule_policy = {"failed_item_max_retries": 0, "failed_item_retry_minutes": 0}
        scheduler = Scheduler(config, store=self.store, action_runner=runner, clock=lambda: clock_now)
        scheduler.schedule("flaky", "note_save", "2026-05-28T11:00:00", arguments={})
        result = scheduler.run_due(now=clock_now)
        self.assertEqual(result["ran"][0]["status"], "failed")
        items = self.store.all_items()
        self.assertEqual(items[0]["status"], "failed")

    def test_cancel_marks_cancelled_without_running(self):
        clock_now = datetime(2026, 5, 28, 12, 0, 0)
        scheduler = self._scheduler({"note_save"}, clock=lambda: clock_now)
        record = scheduler.schedule("c", "note_save", "2026-05-28T11:00:00", arguments={})
        self.assertTrue(scheduler.cancel(record["id"]))
        result = scheduler.run_due(now=clock_now)
        self.assertEqual(result["ran_count"], 0)
        items = self.store.all_items()
        self.assertEqual(items[0]["status"], "cancelled")

    def test_run_due_skips_when_action_drops_off_whitelist(self):
        clock_now = datetime(2026, 5, 28, 12, 0, 0)
        scheduler = self._scheduler({"note_save"}, clock=lambda: clock_now)
        scheduler.schedule("ok", "note_save", "2026-05-28T11:00:00", arguments={})
        # mutate whitelist after scheduling
        scheduler._allowed_actions = set()
        result = scheduler.run_due(now=clock_now)
        self.assertEqual(result["ran_count"], 0)
        self.assertEqual(result["skipped"][0]["reason"], "action_not_whitelisted")
        items = self.store.all_items()
        self.assertEqual(items[0]["status"], "skipped")

    def test_link_to_memory_persists_related_id(self):
        note_writer, memory_writer, note_calls, memory_calls = _stub_writers()
        config = _config_with_whitelist(self.root, {"note_save"})
        config.memory_linkage = {"remember_on_create": True, "importance_default": 0.5}
        scheduler = Scheduler(
            config,
            store=self.store,
            action_runner=lambda a, args: {"ok": True},
            note_writer=note_writer,
            memory_writer=memory_writer,
            clock=lambda: datetime(2026, 5, 28, 10, 0, 0),
        )
        record = scheduler.schedule("with-mem", "note_save", "2026-05-28T12:00:00", arguments={})
        self.assertTrue(memory_calls)
        stored = self.store.get_item(record["id"])
        self.assertIsNotNone(stored.get("related_memory_id"))

    def test_link_to_note_persists_related_title(self):
        note_writer, memory_writer, note_calls, memory_calls = _stub_writers()
        config = _config_with_whitelist(self.root, {"note_save"})
        config.notes_linkage = {"note_on_complete": True, "note_title_prefix": "Sched: ", "note_tags": "scheduled"}
        scheduler = Scheduler(
            config,
            store=self.store,
            action_runner=lambda a, args: {"ok": True},
            note_writer=note_writer,
            memory_writer=memory_writer,
            clock=lambda: datetime(2026, 5, 28, 10, 0, 0),
        )
        record = scheduler.schedule("note-link", "note_save", "2026-05-28T12:00:00", arguments={})
        self.assertTrue(note_calls)
        stored = self.store.get_item(record["id"])
        self.assertIsNotNone(stored.get("related_note_title"))


class SchedulerToolRegistrationTests(unittest.TestCase):
    def test_tools_registered(self):
        import tools  # noqa: F401
        from tools.registry import get_tool

        for name in ("scheduler_schedule", "scheduler_list", "scheduler_cancel", "scheduler_run_due", "daily_maintenance"):
            self.assertIsNotNone(get_tool(name), f"tool {name} not registered")


if __name__ == "__main__":
    unittest.main()
