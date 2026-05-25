import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from memory.summaries import SummaryStore


class TestSummaryStore(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.path = os.path.join(self.tmpdir, "summaries.json")
        self.store = SummaryStore(self.path, max_summaries=5)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    # --- Basic CRUD ---

    def test_empty(self):
        self.assertEqual(self.store.size(), 0)
        self.assertEqual(self.store.get_recent(), [])
        self.assertEqual(self.store.context_string(), "")

    def test_append_one(self):
        count = self.store.append("First summary", turn_count=3)
        self.assertEqual(count, 1)
        self.assertEqual(self.store.size(), 1)
        recent = self.store.get_recent(1)
        self.assertEqual(len(recent), 1)
        self.assertEqual(recent[0]["text"], "First summary")
        self.assertEqual(recent[0]["turn_count"], 3)
        self.assertIn("timestamp", recent[0])
        self.assertIn("start_date", recent[0])

    def test_append_skips_json_tool_call_text(self):
        count = self.store.append('{"name":"fetch_latest_reddit_threads","parameters":{}}')

        self.assertEqual(count, 0)
        self.assertEqual(self.store.size(), 0)

    def test_append_empty_text(self):
        count = self.store.append("", turn_count=0)
        self.assertEqual(count, 0)
        self.assertEqual(self.store.size(), 0)

    def test_append_whitespace(self):
        count = self.store.append("   ", turn_count=1)
        self.assertEqual(count, 0)
        self.assertEqual(self.store.size(), 0)

    def test_append_multiple(self):
        for i in range(3):
            self.store.append(f"Summary {i}", turn_count=i)
        self.assertEqual(self.store.size(), 3)
        recent = self.store.get_recent(5)
        self.assertEqual(len(recent), 3)

    # --- Trimming ---

    def test_trim_exceeds_max(self):
        for i in range(10):
            self.store.append(f"Summary {i}", turn_count=1)
        self.assertEqual(self.store.size(), 5)
        texts = [s["text"] for s in self.store.summaries]
        self.assertEqual(texts, [f"Summary {i}" for i in range(5, 10)])

    def test_max_summaries_at_least_one(self):
        store = SummaryStore(self.path, max_summaries=0)
        self.assertEqual(store.max_summaries, 1)
        store2 = SummaryStore(self.path, max_summaries=-5)
        self.assertEqual(store2.max_summaries, 1)

    # --- context_string ---

    def test_context_string_multiple(self):
        self.store.append("User likes apples", turn_count=2)
        self.store.append("User is learning Python", turn_count=3)
        text = self.store.context_string(n=5)
        self.assertIn("Previous session summaries:", text)
        self.assertIn("User likes apples", text)
        self.assertIn("User is learning Python", text)

    def test_context_string_empty(self):
        self.assertEqual(self.store.context_string(), "")

    def test_context_string_skips_legacy_json_tool_call_text(self):
        self.store.summaries.append({
            "text": '{"function":{"name":"reddit","arguments":"{}"}}',
            "turn_count": 1,
            "timestamp": "now",
            "start_date": "today",
        })

        self.assertEqual(self.store.context_string(), "")

    def test_context_string_limits_n(self):
        for i in range(10):
            self.store.append(f"Summary {i}", turn_count=1)
        text = self.store.context_string(n=2)
        self.assertIn("Summary 8", text)
        self.assertIn("Summary 9", text)
        self.assertNotIn("Summary 7", text)

    def test_context_string_zero_n(self):
        self.store.append("test", turn_count=1)
        self.assertEqual(self.store.context_string(0), "")

    def test_system_prompt_includes_summary_context(self):
        from agent.core import _build_system_prompt

        summary_context = "Previous session summaries:\n- User's name is Krish Verma"
        prompt = _build_system_prompt([], summary_context=summary_context)
        self.assertIn("Previous session summaries:", prompt)
        self.assertIn("User's name is Krish Verma", prompt)

    def test_agent_initial_prompt_keeps_persisted_summary_context(self):
        from agent import core

        self.store.append("User's name is Krish Verma", turn_count=2)
        original_path = core.settings.summaries_path
        original_max_count = core.settings.summaries_max_count
        original_max_context = core.settings.summaries_max_context
        original_check_api_key = core.NIMClient.check_api_key
        original_brain = core.Brain
        try:
            core.settings.summaries_path = self.path
            core.settings.summaries_max_count = 5
            core.settings.summaries_max_context = 3
            core.NIMClient.check_api_key = lambda _client: True
            core.Brain = lambda: object()

            agent = core.Agent()

            self.assertIn("Previous session summaries:", agent.messages[0]["content"])
            self.assertIn("User's name is Krish Verma", agent.messages[0]["content"])
            rebuilt = core._build_system_prompt([], summary_context=agent._summary_context)
            self.assertIn("User's name is Krish Verma", rebuilt)
        finally:
            core.settings.summaries_path = original_path
            core.settings.summaries_max_count = original_max_count
            core.settings.summaries_max_context = original_max_context
            core.NIMClient.check_api_key = original_check_api_key
            core.Brain = original_brain

    # --- Clear ---

    def test_clear(self):
        self.store.append("test", turn_count=1)
        self.store.clear()
        self.assertEqual(self.store.size(), 0)
        self.assertEqual(self.store.summaries, [])

    # --- Persistence ---

    def test_save_and_load(self):
        self.store.append("Saved summary", turn_count=5)
        self.store.save()

        store2 = SummaryStore(self.path, max_summaries=5)
        self.assertTrue(store2.load())
        self.assertEqual(store2.size(), 1)
        self.assertEqual(store2.summaries[0]["text"], "Saved summary")
        self.assertEqual(store2.summaries[0]["turn_count"], 5)

    def test_load_missing_file(self):
        store2 = SummaryStore("nonexistent.json", max_summaries=5)
        self.assertFalse(store2.load())
        self.assertEqual(store2.size(), 0)

    def test_load_corrupted_file(self):
        self.path = os.path.join(self.tmpdir, "broken.json")
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("not json")
        store2 = SummaryStore(self.path, max_summaries=5)
        self.assertFalse(store2.load())

    def test_load_empty_json(self):
        self.path = os.path.join(self.tmpdir, "empty.json")
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({}, f)
        store2 = SummaryStore(self.path, max_summaries=5)
        self.assertFalse(store2.load())

    def test_load_legacy_list_format(self):
        self.path = os.path.join(self.tmpdir, "legacy.json")
        data = [{"text": "legacy summary", "turn_count": 1, "timestamp": "now", "start_date": "today"}]
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(data, f)
        store2 = SummaryStore(self.path, max_summaries=5)
        self.assertTrue(store2.load())
        self.assertEqual(store2.size(), 1)
        self.assertEqual(store2.summaries[0]["text"], "legacy summary")

    def test_persistence_across_instances(self):
        self.store.append("First", turn_count=1)
        self.store.append("Second", turn_count=2)
        self.store.save()

        store2 = SummaryStore(self.path, max_summaries=5)
        store2.load()
        self.assertEqual(store2.size(), 2)
        self.assertEqual(store2.summaries[0]["text"], "First")
        self.assertEqual(store2.summaries[1]["text"], "Second")

    def test_save_creates_parent_dir(self):
        deep_path = os.path.join(self.tmpdir, "sub", "nested", "summaries.json")
        store = SummaryStore(deep_path, max_summaries=5)
        store.append("Deep test", turn_count=1)
        self.assertTrue(os.path.exists(deep_path))

    # --- Edge Cases ---

    def test_append_after_load_preserves_entries(self):
        self.store.append("Before", turn_count=1)
        self.store.save()
        self.store.append("After", turn_count=2)
        self.assertEqual(self.store.size(), 2)
        self.assertEqual(self.store.summaries[0]["text"], "Before")
        self.assertEqual(self.store.summaries[1]["text"], "After")

    def test_get_recent_with_n_less_than_count(self):
        for i in range(3):
            self.store.append(f"S{i}", turn_count=1)
        recent = self.store.get_recent(2)
        self.assertEqual(len(recent), 2)
        self.assertEqual(recent[0]["text"], "S1")
        self.assertEqual(recent[1]["text"], "S2")

    def test_get_recent_negative_n(self):
        self.store.append("test", turn_count=1)
        self.assertEqual(self.store.get_recent(-1), [])

    def test_get_recent_non_integer_string_n(self):
        self.store.append("test", turn_count=1)
        self.assertEqual(self.store.get_recent("bad"), [])

    def test_get_recent_float_n(self):
        self.store.append("test", turn_count=1)
        recent = self.store.get_recent(0.5)
        self.assertEqual(len(recent), 0)


if __name__ == "__main__":
    unittest.main()
