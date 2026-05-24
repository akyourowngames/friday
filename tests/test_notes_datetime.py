import io
import json
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tools
from tools.datetime_tool import datetime_info
from tools import notes as notes_mod
from tools.notes import note_delete, note_list, note_read, note_save, note_search, note_update
from tools.registry import get_tool


class NotesToolUpgradeTests(unittest.TestCase):
    def setUp(self):
        self._original_path = notes_mod.NOTES_FILE
        self._path = Path("storage") / "notes_upgrade_test.json"
        self._path.parent.mkdir(parents=True, exist_ok=True)
        notes_mod.NOTES_FILE = self._path

    def tearDown(self):
        notes_mod.NOTES_FILE = self._original_path
        if self._path.exists():
            self._path.unlink()

    def test_tools_registered(self):
        self.assertIsNotNone(tools)
        for name in ("note_save", "note_read", "note_update", "note_delete", "note_list", "note_search"):
            self.assertIsNotNone(get_tool(name))

    def test_note_save_structured_trace(self):
        stream = io.StringIO()
        with redirect_stdout(stream):
            result = note_save(
                "upgrade test",
                "body",
                tags="work, demo",
                response_format="structured",
                trace_enabled=True,
            )

        trace = json.loads(stream.getvalue().strip())
        self.assertEqual(result["result"]["action"], "created")
        self.assertEqual(result["result"]["tags"], ["work", "demo"])
        self.assertEqual(trace["tool"], "note_save")
        self.assertEqual(trace["status"], "SUCCESS")

    def test_note_read_legacy_unchanged_shape(self):
        note_save("legacy read", "hello world", response_format="legacy")
        text = note_read("legacy read", response_format="legacy")
        self.assertIn("hello world", text)
        self.assertIn("Created:", text)

    def test_note_search_respects_limit(self):
        for idx in range(5):
            note_save(f"batch {idx}", f"shared keyword item {idx}", response_format="legacy")
        result = note_search("keyword", limit=2, response_format="structured")
        self.assertEqual(result["result"]["count"], 2)
        self.assertTrue(result["result"]["truncated"])
        self.assertEqual(result["result"]["total_matches"], 5)

    def test_empty_title_returns_structured_error(self):
        result = note_save("", "x", response_format="structured")
        self.assertEqual(result["error"]["code"], "EMPTY_TITLE")

    def test_ambiguous_delete_legacy_preserved(self):
        self._path.write_text(
            '{"project alpha":{"content":"a","created":"x","updated":"x","tags":[]},'
            '"project beta":{"content":"b","created":"x","updated":"x","tags":[]}}',
            encoding="utf-8",
        )
        result = note_delete("project", response_format="legacy")
        self.assertIn("Ambiguous note title", result)
        data = self._path.read_text(encoding="utf-8")
        self.assertIn("project alpha", data)

    def test_unique_partial_update_legacy_preserved(self):
        self._path.write_text(
            '{"project alpha":{"content":"a","created":"x","updated":"x","tags":[]},'
            '"meeting":{"content":"b","created":"x","updated":"x","tags":[]}}',
            encoding="utf-8",
        )
        result = note_update("alpha", content="changed", response_format="legacy")
        self.assertIn("Updated note 'project alpha'", result)
        self.assertIn("changed", self._path.read_text(encoding="utf-8"))

    def test_note_list_tag_filter_structured(self):
        note_save("food note", "pasta", tags="food", response_format="legacy")
        note_save("work note", "tasks", tags="work", response_format="legacy")
        result = note_list(tag="food", response_format="structured")
        self.assertEqual(result["result"]["count"], 1)
        self.assertEqual(result["result"]["notes"][0]["title"], "food note")


class DateTimeToolUpgradeTests(unittest.TestCase):
    def test_city_timezone_resolves_legacy(self):
        result = datetime_info("Tokyo", response_format="legacy")
        self.assertNotIn("Unknown timezone", result)
        self.assertIn("Asia/Tokyo", result)

    def test_unknown_timezone_legacy(self):
        result = datetime_info("Not A Timezone", response_format="legacy")
        self.assertIn("Unknown timezone", result)

    def test_structured_iso_output(self):
        result = datetime_info("UTC", output_style="iso", response_format="structured")
        self.assertEqual(result["result"]["timezone_resolved"], "UTC")
        self.assertIn("T", result["result"]["iso"])
        self.assertEqual(result["result"]["output_style"], "iso")

    def test_ambiguous_timezone_structured(self):
        result = datetime_info("America", response_format="structured")
        self.assertEqual(result["result"]["status"], "ambiguous")
        self.assertGreater(result["result"]["match_count"], 1)

    def test_invalid_output_style(self):
        result = datetime_info("UTC", output_style="verbose", response_format="structured")
        self.assertEqual(result["error"]["code"], "INVALID_OUTPUT_STYLE")

    def test_trace_emitted_on_success(self):
        stream = io.StringIO()
        with redirect_stdout(stream):
            datetime_info("local", response_format="structured", trace_enabled=True)
        trace = json.loads(stream.getvalue().strip())
        self.assertEqual(trace["tool"], "datetime_info")
        self.assertEqual(trace["status"], "SUCCESS")


if __name__ == "__main__":
    unittest.main()
