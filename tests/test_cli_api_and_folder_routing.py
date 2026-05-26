import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tools  # noqa: F401
from agent.core import (
    _compact_tool_result_for_context,
    _embedding_query,
    _forced_folder_watcher_call,
    _prefer_folder_watcher_for_folder_context,
)
from agent.router import ToolRouter
from api_server import _panel_payload
from main import _iter_sse_events, _normalize_api_base
from tools.registry import get_tool_schemas


class FakeSseResponse:
    def __init__(self, lines):
        self._lines = lines

    def iter_lines(self):
        return iter(self._lines)


class CliApiAndFolderRoutingTests(unittest.TestCase):
    def test_api_base_normalization(self):
        self.assertEqual(_normalize_api_base("http://127.0.0.1:8011/"), "http://127.0.0.1:8011")
        self.assertEqual(_normalize_api_base(""), "http://127.0.0.1:8000")

    def test_sse_event_parser_reads_json_data_lines(self):
        response = FakeSseResponse(
            [
                "",
                "data: " + json.dumps({"chunk": "hello"}),
                "event: ignored",
                b"data: " + json.dumps({"done": True}).encode("utf-8"),
            ]
        )

        events = list(_iter_sse_events(response))

        self.assertEqual(events, [{"chunk": "hello"}, {"done": True}])

    def test_natural_folder_count_prompts_prefer_folder_watcher(self):
        for prompt in (
            "what is in the folder?",
            "ok tell me how many python files are there and images",
        ):
            with self.subTest(prompt=prompt):
                q_emb, routing_input = _embedding_query(prompt, [])
                selected = ToolRouter().select_tools(routing_input, q_emb)
                preferred = _prefer_folder_watcher_for_folder_context(prompt, q_emb, selected)

                self.assertEqual(preferred[0]["name"], "folder_watcher")
                self.assertNotIn("file_list", [tool["name"] for tool in preferred])

    def test_raw_directory_listing_stays_file_list(self):
        prompt = "show directory entries in tools"
        q_emb, routing_input = _embedding_query(prompt, [])
        selected = ToolRouter().select_tools(routing_input, q_emb)
        preferred = _prefer_folder_watcher_for_folder_context(prompt, q_emb, selected)

        self.assertEqual(preferred[0]["name"], "file_list")

    def test_single_selected_folder_watcher_is_forced_to_execute(self):
        schema = [item for item in get_tool_schemas() if item["function"]["name"] == "folder_watcher"]
        call = _forced_folder_watcher_call("how many python files are there and images", schema)

        self.assertIsNotNone(call)
        self.assertEqual(call["name"], "folder_watcher")
        self.assertEqual(json.loads(call["arguments"])["action"], "ask")

    def test_folder_watcher_tool_context_stays_panel_parseable_after_compaction(self):
        big_file = {
            "id": "file-1",
            "filename": "large.py",
            "extension": ".py",
            "mime_type": "text/x-python",
            "size_bytes": 123,
            "content_excerpt": "x" * 2000,
            "metadata": {"large": "y" * 2000},
        }
        raw = {
            "result": {
                "action": "ask",
                "query": "how many python files",
                "answer": "There is one Python file.",
                "mode": "llm_chat",
                "data": {
                    "answer": "There is one Python file.",
                    "stats": {"active_files": 1, "total_size_bytes": 123, "by_extension_details": {".py": {"count": 1, "size_bytes": 123}}},
                    "files": [big_file],
                    "largest_files": [big_file] * 20,
                },
                "stats": {"active_files": 1, "total_size_bytes": 123, "by_extension_details": {".py": {"count": 1, "size_bytes": 123}}},
                "files": [big_file] * 20,
                "count": 1,
            },
            "meta": {"tool": "folder_watcher"},
        }

        compact = _compact_tool_result_for_context(raw)
        parsed = json.loads(json.dumps(compact))
        panel = _panel_payload("folder_watcher", parsed)

        self.assertEqual(panel["source"], "folder_watcher")
        self.assertEqual(panel["stats"]["by_extension_details"][".py"]["count"], 1)
        self.assertLessEqual(len(parsed["result"]["files"]), 8)


if __name__ == "__main__":
    unittest.main()
