import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tools
from tools.files import file_list
from tools.image import imagine, images_manage
from tools.manifest_audit import tool_manifest_audit
from tools.registry import execute_tool, get_tool


class ToolsFleetPolishTests(unittest.TestCase):
    def test_memory_and_image_tools_registered(self):
        for name in ("memory_assess", "memory_recall", "memory_remember", "memory_forget", "imagine", "gallery", "camera_vision", "composio", "folder_watcher"):
            self.assertIsNotNone(get_tool(name))

    def test_execute_tool_allows_tool_parameter_named_name(self):
        result = execute_tool("keyboard_shortcut", name="missing_smoke_shortcut", response_format="structured")

        self.assertEqual(result["error"]["code"], "SHORTCUT_NOT_FOUND")

    def test_file_list_uses_markdown_path_alias(self):
        result = file_list("current folder", limit=5, response_format="structured")

        self.assertIn("result", result)
        self.assertTrue(result["result"]["items"])

    def test_img_abbreviation_selects_imagine(self):
        from agent.core import _embedding_query
        from agent.router import ToolRouter

        router = ToolRouter()
        q_emb, routing_input = _embedding_query("gen me img of girl", [])
        selected = router.select_tools(routing_input, q_emb)

        self.assertIn("imagine", [tool["name"] for tool in selected])

    def test_new_reddit_request_after_image_context_selects_reddit(self):
        from agent.core import _embedding_query
        from agent.router import ToolRouter

        messages = [{"role": "user", "content": "gen me img of super car and open it up for me"}]
        q_emb, routing_input = _embedding_query(
            "ahh cool now show me project hail mary reddit threads",
            messages,
        )
        selected = ToolRouter().select_tools(routing_input, q_emb)
        names = [tool["name"] for tool in selected]

        self.assertIn("reddit", names)
        self.assertNotEqual(names[0], "gallery")

    def test_markdown_file_path_selects_file_read(self):
        from agent.core import _embedding_query
        from agent.router import ToolRouter

        q_emb, routing_input = _embedding_query("read routing_policy.md", [])
        selected = ToolRouter().select_tools(routing_input, q_emb)

        self.assertIn("file_read", [tool["name"] for tool in selected])

    def test_folder_watcher_request_selects_folder_watcher_while_casual_chat_does_not(self):
        from agent.core import _embedding_query
        from agent.router import ToolRouter

        router = ToolRouter()
        q_emb, routing_input = _embedding_query("ask folder watcher for the total size of python files", [])
        selected = router.select_tools(routing_input, q_emb)
        casual_selected = router.select_tools("how are you doing")

        self.assertIn("folder_watcher", [tool["name"] for tool in selected])
        self.assertNotIn("folder_watcher", [tool["name"] for tool in casual_selected])

    def test_reddit_clamps_hallucinated_large_limit(self):
        import tools.reddit as reddit_mod

        original = reddit_mod._run_reddit
        seen = {}

        def fake_run(action, subreddit, query, limit, time_filter, sort, timeout_seconds, stats):
            seen["limit"] = limit
            return reddit_mod._operation_result(
                action,
                "ok",
                [{"title": "ok"}],
                extra={"query": query, "sort": sort},
            )

        try:
            reddit_mod._run_reddit = fake_run
            result = reddit_mod.reddit(action="search", query="ai", limit="200", response_format="structured")
        finally:
            reddit_mod._run_reddit = original

        self.assertEqual(seen["limit"], 25)
        self.assertEqual(result["result"]["count"], 1)

    def test_reddit_front_with_query_becomes_search(self):
        import tools.reddit as reddit_mod

        original = reddit_mod._run_reddit
        seen = {}

        def fake_run(action, subreddit, query, limit, time_filter, sort, timeout_seconds, stats):
            seen["action"] = action
            seen["query"] = query
            return reddit_mod._operation_result(
                action,
                "ok",
                [{"title": "ok"}],
                extra={"query": query, "sort": sort},
            )

        try:
            reddit_mod._run_reddit = fake_run
            result = reddit_mod.reddit(action="front", query="ai", response_format="structured")
        finally:
            reddit_mod._run_reddit = original

        self.assertEqual(seen["action"], "search")
        self.assertEqual(seen["query"], "ai")
        self.assertEqual(result["result"]["action"], "search")

    def test_reddit_new_without_subreddit_uses_global_listing(self):
        import tools.reddit as reddit_mod

        original = reddit_mod._run_reddit
        seen = {}

        def fake_run(action, subreddit, query, limit, time_filter, sort, timeout_seconds, stats):
            seen["action"] = action
            seen["subreddit"] = subreddit
            return reddit_mod._operation_result(action, "ok", [{"title": "ok"}])

        try:
            reddit_mod._run_reddit = fake_run
            result = reddit_mod.reddit(action="new", response_format="structured")
        finally:
            reddit_mod._run_reddit = original

        self.assertEqual(seen["action"], "new")
        self.assertEqual(seen["subreddit"], "")
        self.assertEqual(result["result"]["action"], "new")

    def test_hackernews_clamps_hallucinated_large_limit(self):
        import tools.hackernews as hn_mod

        original = hn_mod._run_hn
        seen = {}

        def fake_run(action, limit, query, timeout_seconds, stats):
            seen["limit"] = limit
            return hn_mod._operation_result(action, "ok", [{"title": "ok"}], extra={"query": query})

        try:
            hn_mod._run_hn = fake_run
            result = hn_mod.hackernews(action="top", limit="200", response_format="structured")
        finally:
            hn_mod._run_hn = original

        self.assertEqual(seen["limit"], 30)
        self.assertEqual(result["result"]["count"], 1)

    def test_hackernews_fetch_alias_uses_top(self):
        import tools.hackernews as hn_mod

        original = hn_mod._run_hn
        seen = {}

        def fake_run(action, limit, query, timeout_seconds, stats):
            seen["action"] = action
            return hn_mod._operation_result(action, "ok", [{"title": "ok"}], extra={"query": query})

        try:
            hn_mod._run_hn = fake_run
            result = hn_mod.hackernews(action="fetch", response_format="structured")
        finally:
            hn_mod._run_hn = original

        self.assertEqual(seen["action"], "top")
        self.assertEqual(result["result"]["action"], "top")

    def test_manifest_audit_structured_success(self):
        result = tool_manifest_audit(".", response_format="structured", include_schema=True)
        self.assertIn(result["result"]["status"], ("success", "partial"))
        self.assertIn("observed_modules", result["result"])

    def test_imagine_rejects_short_prompt_structured(self):
        result = imagine("hi", response_format="structured")
        self.assertEqual(result["error"]["code"], "SHORT_PROMPT")

    def test_imagine_accepts_four_character_prompt(self):
        import tools.image as image_mod

        original_generate = image_mod._generate
        original_save = image_mod._save_image
        original_open = image_mod._open_image

        def fake_generate(prompt, width, height, model):
            return "ZmFrZQ==", "fake", ""

        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "girl.png"

            def fake_save(_b64_data, _prompt_slug):
                output.write_bytes(b"fake")
                return output

            try:
                image_mod._generate = fake_generate
                image_mod._save_image = fake_save
                image_mod._open_image = lambda _path: False
                result = imagine("girl", open_viewer=False, response_format="structured")
            finally:
                image_mod._generate = original_generate
                image_mod._save_image = original_save
                image_mod._open_image = original_open

        self.assertIn("result", result)
        self.assertEqual(result["result"]["provider_used"], "fake")

    def test_gallery_list_structured_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            original = Path(__file__).resolve().parent.parent / "tools" / "image.py"
            import tools.image as image_mod

            old_dir = image_mod._IMAGE_DIR
            image_mod._IMAGE_DIR = Path(tmp)
            try:
                result = images_manage("list", response_format="structured")
            finally:
                image_mod._IMAGE_DIR = old_dir
        self.assertEqual(result["result"]["count"], 0)

    def test_gallery_trace_emits_json(self):
        stream = io.StringIO()
        with redirect_stdout(stream):
            tool_manifest_audit(".", response_format="legacy", trace_enabled=True, max_items=20)
        trace = json.loads(stream.getvalue().strip())
        self.assertEqual(trace["tool"], "tool_manifest_audit")


if __name__ == "__main__":
    unittest.main()
