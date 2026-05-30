"""Tests for the memory-driven scheduler bridge."""

import json
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import memory.scheduler_bridge as bridge
from config import settings


def _fake_llm(payload):
    """Build a fake NIMClient whose chat completion returns the given JSON."""
    text = json.dumps(payload)

    class _Completions:
        def create(self, **kwargs):
            msg = SimpleNamespace(content=text)
            return SimpleNamespace(choices=[SimpleNamespace(message=msg)])

    class _Chat:
        completions = _Completions()

    return SimpleNamespace(client=SimpleNamespace(chat=_Chat()))


class _FakeBrain:
    def __init__(self, memories):
        self.memories = memories


class ExtractionTests(unittest.TestCase):
    def test_recent_facts_filters_old(self):
        today = date(2026, 5, 29)
        memories = [
            {"text": "old fact", "_date": "2026-01-01"},
            {"text": "recent fact", "_date": "2026-05-20"},
        ]
        facts = bridge._recent_facts(memories, recent_days=21, lookback=40, today=today)
        self.assertIn("recent fact", facts)
        self.assertNotIn("old fact", facts)

    def test_extract_resolves_and_filters_dates(self):
        today = date(2026, 5, 29)
        client = _fake_llm([
            {"task": "Call the dentist", "date": "2026-05-30"},
            {"task": "Past thing", "date": "2026-05-01"},   # dropped (past)
            {"task": "No date thing", "date": ""},          # dropped (no date)
        ])
        out = bridge._extract_commitments(["x"], today, max_tokens=200, llm_client=client)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["task"], "Call the dentist")
        self.assertEqual(out[0]["date"], date(2026, 5, 30))


class RunBridgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self._orig_store = settings.scheduler_store_path
        self._orig_log = settings.scheduler_log_path
        self._orig_enabled = settings.memory_scheduler_bridge_enabled
        settings.scheduler_store_path = str(root / "sched.json")
        settings.scheduler_log_path = str(root / "sched_log.jsonl")
        settings.memory_scheduler_bridge_enabled = True
        self.addCleanup(self._restore)

    def _restore(self):
        settings.scheduler_store_path = self._orig_store
        settings.scheduler_log_path = self._orig_log
        settings.memory_scheduler_bridge_enabled = self._orig_enabled

    def test_schedules_nudge_and_is_idempotent(self):
        now = datetime(2026, 5, 29, 8, 0, 0)
        future = (now.date() + timedelta(days=2)).isoformat()
        brain = _FakeBrain([{"text": "I should call the dentist soon", "_date": now.date().isoformat()}])
        client = _fake_llm([{"task": "Call the dentist", "date": future}])

        result = bridge.run_bridge(brain, now=now, llm_client=client)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["scheduled"], 1)

        # Second run with the same commitment must not create a duplicate.
        result2 = bridge.run_bridge(brain, now=now, llm_client=client)
        self.assertEqual(result2["scheduled"], 0)

        from scheduler.engine import build_scheduler
        from tools.reminder import _reminder_actions

        scheduler = build_scheduler(allowed_actions=_reminder_actions())
        pending = [i for i in scheduler.list_items(status="pending") if "memory_nudge" in (i.get("tags") or [])]
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["arguments"]["task"], "Call the dentist")

    def test_disabled_returns_no_op(self):
        settings.memory_scheduler_bridge_enabled = False
        result = bridge.run_bridge(_FakeBrain([]), now=datetime(2026, 5, 29))
        self.assertEqual(result["status"], "disabled")

    def test_no_commitments_schedules_nothing(self):
        now = datetime(2026, 5, 29, 8, 0, 0)
        brain = _FakeBrain([{"text": "User likes tea", "_date": now.date().isoformat()}])
        client = _fake_llm([])
        result = bridge.run_bridge(brain, now=now, llm_client=client)
        self.assertEqual(result["scheduled"], 0)


if __name__ == "__main__":
    unittest.main()
