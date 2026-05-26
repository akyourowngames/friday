import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tools  # noqa: F401
import tools.folder_watcher as watcher_tool
from tools.registry import execute_tool, get_tool


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text

    def json(self):
        return self._payload


def _client_config(enabled_actions=None, action_semantics=None):
    return watcher_tool.FolderWatcherClientConfig(
        path=Path("client.md"),
        active_target="demo",
        default_timeout_ms=1200,
        max_limit=50,
        targets={"demo": watcher_tool.FolderWatcherTarget("demo", "http://watcher.test", "KING_FOLDER_WATCHER_TEST_TOKEN")},
        enabled_actions=set(enabled_actions or watcher_tool._ACTIONS),
        action_semantics=action_semantics or {},
    )


class FolderWatcherToolTests(unittest.TestCase):
    def test_tool_registered_with_expected_schema(self):
        info = get_tool("folder_watcher")

        self.assertIsNotNone(info)
        properties = info["parameters"]["properties"]
        for name in ("action", "query", "file_id", "extension", "directory", "limit", "response_format"):
            self.assertIn(name, properties)

    def test_client_config_parses_markdown_targets_actions_and_runtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "client.md"
            config_path.write_text(
                "\n".join(
                    [
                        "# Client",
                        "## Runtime",
                        "- active_target: demo",
                        "- default_timeout_ms: 9000",
                        "- max_limit: 77",
                        "## Targets",
                        "- demo: http://127.0.0.1:7475 | auth_env: KING_FOLDER_WATCHER_TOKEN",
                        "- local: http://127.0.0.1:7474",
                        "## Enabled Actions",
                        "- ask",
                        "- stats",
                        "## Action Semantics",
                        "- ask: broad folder questions",
                        "- stats: aggregate folder counts and sizes",
                    ]
                ),
                encoding="utf-8",
            )

            config = watcher_tool._load_client_config(root, config_path)

        self.assertEqual(config.active_target, "demo")
        self.assertEqual(config.default_timeout_ms, 9000)
        self.assertEqual(config.max_limit, 77)
        self.assertEqual(config.targets["demo"].base_url, "http://127.0.0.1:7475")
        self.assertEqual(config.targets["demo"].auth_env, "KING_FOLDER_WATCHER_TOKEN")
        self.assertEqual(config.enabled_actions, {"ask", "stats"})
        self.assertEqual(config.action_semantics["stats"], "aggregate folder counts and sizes")

    def test_natural_builder_uses_markdown_action_semantics(self):
        config = _client_config(
            action_semantics={
                "ask": "broad folder answer",
                "stats": "aggregate counts sizes and file type totals",
                "latest": "recent indexed files",
            }
        )

        def fake_scores(user_input, candidates):
            return [(action, 0.91 if action == "stats" else 0.2) for action, _ in candidates]

        with patch.object(watcher_tool, "_load_client_config", return_value=config):
            with patch.object(watcher_tool, "_score_action_semantics", side_effect=fake_scores):
                args = watcher_tool.build_natural_folder_watcher_args("how many python files and images")

        self.assertEqual(args["action"], "stats")
        self.assertEqual(args["query"], "how many python files and images")
        self.assertEqual(args["response_format"], "structured")

    def test_natural_builder_respects_enabled_actions(self):
        config = _client_config(
            enabled_actions={"ask"},
            action_semantics={
                "ask": "broad folder answer",
                "stats": "aggregate counts sizes and file type totals",
            },
        )
        seen_candidates = []

        def fake_scores(user_input, candidates):
            seen_candidates.extend(action for action, _ in candidates)
            return [(action, 0.8) for action, _ in candidates]

        with patch.object(watcher_tool, "_load_client_config", return_value=config):
            with patch.object(watcher_tool, "_score_action_semantics", side_effect=fake_scores):
                args = watcher_tool.build_natural_folder_watcher_args("how many files are there")

        self.assertEqual(seen_candidates, ["ask"])
        self.assertEqual(args["action"], "ask")

    def test_natural_builder_resolves_single_recent_file_for_deep_dive(self):
        config = _client_config(
            action_semantics={
                "ask": "broad folder answer",
                "deep_dive": "detailed analysis for one identified file",
            }
        )

        def fake_scores(user_input, candidates):
            return [(action, 0.95 if action == "deep_dive" else 0.1) for action, _ in candidates]

        recent_single = {
            "result": {
                "files": [{"id": "file-1", "filename": "notes.md"}],
            },
            "meta": {"tool": "folder_watcher"},
        }
        recent_ambiguous = {
            "result": {
                "files": [{"id": "file-1"}, {"id": "file-2"}],
            },
            "meta": {"tool": "folder_watcher"},
        }

        with patch.object(watcher_tool, "_load_client_config", return_value=config):
            with patch.object(watcher_tool, "_score_action_semantics", side_effect=fake_scores):
                single_args = watcher_tool.build_natural_folder_watcher_args("deep dive this file", recent_single)
                missing_args = watcher_tool.build_natural_folder_watcher_args("deep dive this file", None)
                ambiguous_args = watcher_tool.build_natural_folder_watcher_args("deep dive this file", recent_ambiguous)

        self.assertEqual(single_args["action"], "deep_dive")
        self.assertEqual(single_args["file_id"], "file-1")
        self.assertEqual(missing_args["action"], "ask")
        self.assertEqual(ambiguous_args["action"], "ask")

    def test_invalid_action_and_missing_query_return_structured_errors(self):
        with patch.object(watcher_tool, "_load_client_config", return_value=_client_config()):
            invalid = execute_tool("folder_watcher", action="delete", response_format="structured")
            missing_query = execute_tool("folder_watcher", action="ask", response_format="structured")

        self.assertEqual(invalid["error"]["code"], "INVALID_ACTION")
        self.assertEqual(missing_query["error"]["code"], "MISSING_QUERY")

    def test_disabled_action_returns_structured_error(self):
        with patch.object(watcher_tool, "_load_client_config", return_value=_client_config({"stats"})):
            result = execute_tool("folder_watcher", action="ask", query="hello", response_format="structured")

        self.assertEqual(result["error"]["code"], "ACTION_DISABLED")

    def test_mocked_http_actions_call_expected_endpoints(self):
        calls = []

        def fake_request(method, url, params=None, json=None, headers=None, timeout=None):
            calls.append({"method": method, "url": url, "params": params, "json": json, "timeout": timeout})
            path = url.removeprefix("http://watcher.test")
            if path == "/chat":
                return FakeResponse(payload={"answer": "natural answer", "files": []})
            if path == "/files/stats":
                return FakeResponse(payload={"active_files": 2, "total_size_bytes": 123, "by_extension_details": {".py": {"count": 1, "size_bytes": 99}}})
            if path == "/files/query":
                return FakeResponse(payload={"mode": "local_fallback", "query": "python size", "files": [{"id": "q", "filename": "q.py"}]})
            if path == "/files/details":
                return FakeResponse(payload={"count": 1, "files": [{"id": "a", "filename": "a.py"}]})
            if path == "/files/search":
                return FakeResponse(payload={"query": "api", "files": [{"id": "b", "filename": "b.md"}]})
            if path == "/files/latest":
                return FakeResponse(payload={"files": [{"id": "c", "filename": "c.wav"}]})
            if path == "/files/file-1/content":
                return FakeResponse(payload={"file_id": "file-1", "content": "hello"})
            if path == "/files/file-1/deep-dive":
                return FakeResponse(payload={"file_id": "file-1", "answer": "deep", "file": {"id": "file-1", "filename": "deep.py"}})
            if path == "/status":
                return FakeResponse(payload={"implemented": [], "runtime": {"watch_path": "."}})
            return FakeResponse(status_code=404, payload={"detail": path})

        cases = [
            ("ask", {"query": "what is here"}, "POST", "/chat"),
            ("query", {"query": "python size"}, "POST", "/files/query"),
            ("stats", {}, "GET", "/files/stats"),
            ("details", {"extension": "py", "include_content": True}, "GET", "/files/details"),
            ("search", {"query": "api"}, "GET", "/files/search"),
            ("latest", {"extension": "wav"}, "GET", "/files/latest"),
            ("content", {"file_id": "file-1"}, "GET", "/files/file-1/content"),
            ("deep_dive", {"file_id": "file-1"}, "GET", "/files/file-1/deep-dive"),
            ("status", {}, "GET", "/status"),
        ]

        with patch.object(watcher_tool, "_load_client_config", return_value=_client_config()):
            with patch.object(watcher_tool.httpx, "request", side_effect=fake_request):
                for action, kwargs, expected_method, expected_path in cases:
                    with self.subTest(action=action):
                        result = execute_tool("folder_watcher", action=action, response_format="structured", **kwargs)
                        self.assertIn("result", result)
                        self.assertEqual(result["result"]["method"], expected_method)
                        self.assertEqual(result["result"]["endpoint"], expected_path)

        self.assertEqual(len(calls), len(cases))
        details_call = calls[3]
        self.assertEqual(details_call["params"]["ext"], ".py")
        self.assertTrue(details_call["params"]["include_content"])
        ask_call = calls[0]
        self.assertEqual(ask_call["json"]["message"], "what is here")

    def test_service_unavailable_and_auth_failure_are_typed(self):
        with patch.object(watcher_tool, "_load_client_config", return_value=_client_config()):
            with patch.object(watcher_tool.httpx, "request", side_effect=watcher_tool.httpx.ConnectError("down")):
                unavailable = execute_tool("folder_watcher", action="stats", response_format="structured")
            with patch.object(watcher_tool.httpx, "request", return_value=FakeResponse(status_code=401, payload={"detail": "auth token required"})):
                auth = execute_tool("folder_watcher", action="stats", response_format="structured")

        self.assertEqual(unavailable["error"]["code"], "SERVICE_UNAVAILABLE")
        self.assertEqual(unavailable["error"]["base_url"], "http://watcher.test")
        self.assertEqual(auth["error"]["code"], "AUTH_FAILED")
        self.assertNotIn("token required", str(auth["error"].get("suggestion", "")))


class FolderWatcherApiBridgeTests(unittest.TestCase):
    def setUp(self):
        from api_server import app

        self.client = TestClient(app)

    def test_folder_watcher_endpoint_returns_panel_json_from_tool(self):
        tool_result = {
            "result": {
                "action": "stats",
                "query": "",
                "answer": "",
                "data": {"active_files": 3, "total_size_bytes": 456, "by_extension_details": {".py": {"count": 2, "size_bytes": 300}}},
                "stats": {"active_files": 3, "total_size_bytes": 456, "by_extension_details": {".py": {"count": 2, "size_bytes": 300}}},
                "files": [],
                "count": 3,
            },
            "meta": {"tool": "folder_watcher"},
        }
        with patch("api_server.execute_tool", return_value=tool_result) as run_tool:
            response = self.client.post("/folder-watcher", json={"action": "stats"})

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["source"], "folder_watcher")
        self.assertEqual(payload["stats"]["active_files"], 3)
        self.assertTrue(payload["results"])
        self.assertEqual(run_tool.call_args.kwargs["response_format"], "structured")

    def test_folder_watcher_endpoint_executes_registered_tool(self):
        with patch.object(watcher_tool, "_load_client_config", return_value=_client_config()):
            with patch.object(watcher_tool.httpx, "request", return_value=FakeResponse(payload={"active_files": 4, "total_size_bytes": 789})):
                response = self.client.post("/folder-watcher", json={"action": "stats"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["source"], "folder_watcher")
        self.assertEqual(response.json()["stats"]["active_files"], 4)

    def test_folder_watcher_endpoint_maps_service_unavailable(self):
        tool_result = {
            "error": {
                "code": "SERVICE_UNAVAILABLE",
                "message": "Folder watcher service could not be reached.",
                "base_url": "http://watcher.test",
                "endpoint": "/status",
            },
            "meta": {"tool": "folder_watcher"},
        }
        with patch("api_server.execute_tool", return_value=tool_result):
            response = self.client.post("/folder-watcher", json={"action": "status"})

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["detail"]["code"], "SERVICE_UNAVAILABLE")


if __name__ == "__main__":
    unittest.main()
