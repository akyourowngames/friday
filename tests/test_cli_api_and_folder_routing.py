import json
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tools  # noqa: F401
import tools.folder_watcher as watcher_tool
from agent.core import (
    _compact_tool_result_for_context,
    _direct_answer_from_tool_result,
    _embedding_query,
    _filter_tools_for_conversation,
    _forced_folder_watcher_call,
    _prefer_folder_watcher_for_folder_context,
)
from agent.router import ToolRouter
from api_server import _chunk_text, _panel_payload, _run_agent
from main import cmd_memory, _iter_sse_events, _message_from_args, _normalize_api_base, _parse_args, _resolve_api_cli_inputs
from tools.registry import get_tool_schemas


class FakeSseResponse:
    def __init__(self, lines):
        self._lines = lines

    def iter_lines(self):
        return iter(self._lines)


class FakeStreamingAgent:
    def __init__(self):
        self.messages = [{"role": "system", "content": "test"}]

    def process(self, message, emit_chunk=None):
        if emit_chunk:
            emit_chunk("hello ")
            emit_chunk("sir")
        self.messages.append({"role": "user", "content": message})
        self.messages.append({"role": "assistant", "content": "hello sir"})
        return "hello sir"


class FakeConsole:
    def __init__(self):
        self.messages = []

    def print(self, *values, **_kwargs):
        self.messages.append(" ".join(str(value) for value in values))


class FakeMemoryBrain:
    def __init__(self):
        self.maintained = False
        self.synced = False

    def recall_unified(self, query, k=10):
        return [{"text": f"memory for {query}", "score": 0.9}]

    def maintain(self, rebuild=False, backup=False):
        self.maintained = bool(rebuild and backup)
        return {"after": {"tier": "gd"}, "graph_rebuilt": True}

    def _sync_obsidian_graph(self):
        self.synced = True

    def obsidian_graph_status(self):
        return {"status": "synced"}

    def list_memories(self, limit):
        return []

    def system_assessment(self):
        return {"entry_count": 0, "indexed_count": 0, "index_state": "warm", "graph": {"active_edge_count": 0}}


class FakeMemoryAgent:
    def __init__(self):
        self.brain = FakeMemoryBrain()


class CliApiAndFolderRoutingTests(unittest.TestCase):
    def test_api_base_normalization(self):
        self.assertEqual(_normalize_api_base("http://127.0.0.1:8011/"), "http://127.0.0.1:8011")
        self.assertEqual(_normalize_api_base("127.0.0.1:8011/"), "http://127.0.0.1:8011")
        self.assertEqual(_normalize_api_base(""), "http://127.0.0.1:8000")

    def test_api_cli_accepts_natural_one_shot_without_url(self):
        base_url, message = _resolve_api_cli_inputs("what is in this folder?", [])

        self.assertEqual(base_url, "http://127.0.0.1:8000")
        self.assertEqual(message, "what is in this folder?")

    def test_api_cli_accepts_short_natural_one_shot_without_url(self):
        base_url, message = _resolve_api_cli_inputs("hi", [])

        self.assertEqual(base_url, "http://127.0.0.1:8000")
        self.assertEqual(message, "hi")

    def test_api_cli_accepts_explicit_url_and_natural_message(self):
        base_url, message = _resolve_api_cli_inputs("127.0.0.1:8011", ["how many python files are there?"])

        self.assertEqual(base_url, "http://127.0.0.1:8011")
        self.assertEqual(message, "how many python files are there?")

    def test_message_flag_builds_one_shot_text(self):
        args = _parse_args(["--message", "who is rai"])

        self.assertEqual(_message_from_args(args), "who is rai")

    def test_memory_cli_recall_extract_and_sync_paths(self):
        fake_agent = FakeMemoryAgent()
        fake_console = FakeConsole()
        with patch("main.console", fake_console):
            with patch("main._print_table") as print_table:
                cmd_memory(fake_agent, "recall Rai")
                print_table.assert_called()
            with patch("memory.worker.ingest_user_files", return_value={"status": "ok", "user_files_found": 1, "facts_ingested": 2}):
                cmd_memory(fake_agent, "extract")
            cmd_memory(fake_agent, "sync")

        self.assertTrue(fake_agent.brain.maintained)
        self.assertTrue(fake_agent.brain.synced)
        self.assertTrue(any("Memory extract:" in message for message in fake_console.messages))
        self.assertTrue(any("Memory sync:" in message for message in fake_console.messages))

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

    def test_api_text_chunks_preserve_spaces_when_joined(self):
        text = "Good evening, sir. How can I assist you?"

        chunks = _chunk_text(text, target_size=14)

        self.assertEqual("".join(chunks), text)

    def test_run_agent_accepts_stream_callback(self):
        emitted = []
        result = _run_agent(FakeStreamingAgent(), threading.Lock(), "hi", emitted.append)

        self.assertEqual("".join(emitted), "hello sir")
        self.assertEqual(result["response"], "hello sir")

    def test_natural_folder_count_prompts_prefer_folder_watcher(self):
        """Queries about indexed file counts route to folder_watcher via utterances."""
        for prompt in (
            "how many python files are indexed in the watched folder",
            "ok tell me how many python files are there and images",
        ):
            with self.subTest(prompt=prompt):
                q_emb, routing_input = _embedding_query(prompt, [])
                selected = ToolRouter().select_tools(routing_input, q_emb)

                self.assertIn("folder_watcher", [tool["name"] for tool in selected])

    def test_reddit_request_stays_in_external_category(self):
        q_emb, routing_input = _embedding_query("fetch me latest reddit threads", [])
        router = ToolRouter()
        selected = router.select_tools(routing_input, q_emb)
        decision = router.last_decision()

        self.assertEqual(decision.get("category"), "external_retrieval")
        self.assertEqual([tool["name"] for tool in selected], ["reddit"])
        self.assertEqual(router.capability_hint("reddit").get("args", {}).get("action"), "new")
        self.assertTrue(router.capability_hint("reddit").get("direct"))

    def test_telegram_delivery_is_explicit_tool_category(self):
        q_emb, routing_input = _embedding_query("send a file through telegram", [])
        router = ToolRouter()
        selected = router.select_tools(routing_input, q_emb)
        decision = router.last_decision()

        self.assertEqual(decision.get("category"), "telegram_delivery")
        self.assertEqual([tool["name"] for tool in selected], ["telegram_watcher"])

    def test_raw_directory_listing_stays_file_list(self):
        """Raw directory listing queries route to file_list first via utterances."""
        prompt = "show directory entries in tools"
        q_emb, routing_input = _embedding_query(prompt, [])
        selected = ToolRouter().select_tools(routing_input, q_emb)

        self.assertEqual(selected[0]["name"], "file_list")

    def test_casual_chat_does_not_select_folder_watcher(self):
        prompt = "hi how are you bud"
        q_emb, routing_input = _embedding_query(prompt, [])
        selected = ToolRouter().select_tools(routing_input, q_emb)
        filtered = _filter_tools_for_conversation(prompt, q_emb, selected)

        self.assertNotIn("folder_watcher", [tool["name"] for tool in filtered])

    def test_single_selected_folder_watcher_uses_semantic_action(self):
        schema = [item for item in get_tool_schemas() if item["function"]["name"] == "folder_watcher"]
        config = watcher_tool.FolderWatcherClientConfig(
            path=Path("client.md"),
            active_target="demo",
            targets={"demo": watcher_tool.FolderWatcherTarget("demo", "http://watcher.test")},
            enabled_actions=set(watcher_tool._ACTIONS),
            action_semantics={
                "ask": "broad folder answer",
                "stats": "aggregate counts sizes and file type totals",
            },
        )

        def fake_scores(user_input, candidates):
            return [(action, 0.9 if action == "stats" else 0.2) for action, _ in candidates]

        with patch.object(watcher_tool, "_load_client_config", return_value=config):
            with patch.object(watcher_tool, "_score_action_semantics", side_effect=fake_scores):
                call = _forced_folder_watcher_call("how many python files are there and images", schema)

        self.assertIsNotNone(call)
        self.assertEqual(call["name"], "folder_watcher")
        args = json.loads(call["arguments"])
        self.assertEqual(args["action"], "stats")
        self.assertEqual(args["query"], "how many python files are there and images")

    def test_folder_watcher_followup_uses_single_recent_file_id(self):
        schema = [item for item in get_tool_schemas() if item["function"]["name"] == "folder_watcher"]
        config = watcher_tool.FolderWatcherClientConfig(
            path=Path("client.md"),
            active_target="demo",
            targets={"demo": watcher_tool.FolderWatcherTarget("demo", "http://watcher.test")},
            enabled_actions=set(watcher_tool._ACTIONS),
            action_semantics={
                "ask": "broad folder answer",
                "deep_dive": "detailed analysis for one identified file",
            },
        )
        messages = [
            {
                "role": "tool",
                "content": json.dumps({
                    "result": {"files": [{"id": "file-1", "filename": "notes.md"}]},
                    "meta": {"tool": "folder_watcher"},
                }),
            }
        ]

        def fake_scores(user_input, candidates):
            return [(action, 0.9 if action == "deep_dive" else 0.2) for action, _ in candidates]

        with patch.object(watcher_tool, "_load_client_config", return_value=config):
            with patch.object(watcher_tool, "_score_action_semantics", side_effect=fake_scores):
                call = _forced_folder_watcher_call("deep dive this file", schema, messages)

        args = json.loads(call["arguments"])
        self.assertEqual(args["action"], "deep_dive")
        self.assertEqual(args["file_id"], "file-1")

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

    def test_folder_watcher_answer_can_skip_second_llm_rewrite(self):
        payload = {"result": {"answer": "There are 26 Python files."}, "meta": {"tool": "folder_watcher"}}

        answer = _direct_answer_from_tool_result("folder_watcher", json.dumps(payload))

        self.assertEqual(answer, "There are 26 Python files.")

    def test_structured_tool_text_can_skip_second_llm_rewrite(self):
        payload = {"result": {"text": "Reddit fallback text."}, "meta": {"tool": "reddit"}}

        answer = _direct_answer_from_tool_result("reddit", json.dumps(payload))

        self.assertEqual(answer, "Reddit fallback text.")


if __name__ == "__main__":
    unittest.main()
