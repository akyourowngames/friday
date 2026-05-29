import io
import json
import shutil
import sys
import time
import unittest
import webbrowser
from contextlib import redirect_stdout
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.core import (
    _has_backtick_tool_call,
    _json_tool_leak_message,
    _load_tool_policy,
    _forced_hint_tool_call,
    _repair_schema_argument_names,
    _tool_call_grounded,
    _try_parse_json_tool_call,
)
from agent.validator import ToolValidator
from memory.brain import Brain
import memory.brain as brain_mod
from memory.brain import _normalize_fact
from tools.terminal import _detect_shell, _normalize_command, _strip_ansi
import tools.terminal as terminal_mod
from tools.registry import execute_tool, get_tool_schemas, tool
from tools.files import file_read, file_write, file_list
from tools.datetime_tool import datetime_info
from tools.manifest_audit import tool_manifest_audit
import tools.youtube as youtube_mod
import tools.hackernews as hn_mod
import tools.web as web_mod
import tools.notes as notes_mod
from agent.router import ToolRouter, _load_small_talk_text
import agent.router as router_mod


class GroundingTests(unittest.TestCase):
    def _schema(self, name):
        for schema in get_tool_schemas():
            if schema["function"]["name"] == name:
                return schema
        raise AssertionError(f"schema not found: {name}")

    def test_tool_policy_loads_from_markdown_contract(self):
        policy = _load_tool_policy()

        self.assertIn("Tool Grounding Contract", policy)
        self.assertIn("Do not claim live state", policy)

    def test_backtick_shell_command_is_tool_leak_when_terminal_available(self):
        schemas = [self._schema("terminal")]
        self.assertTrue(_has_backtick_tool_call("`start image.png`", schemas))

    def test_backtick_plain_text_is_not_tool_leak_without_terminal(self):
        schemas = [{"function": {"name": "web_search"}}]
        self.assertFalse(_has_backtick_tool_call("Use `hello` as the title.", schemas))

    def test_backtick_plain_text_is_not_tool_leak_with_terminal_available(self):
        schemas = [self._schema("terminal")]
        self.assertFalse(_has_backtick_tool_call("Use `hello` as the title.", schemas))

    def test_windows_start_path_with_spaces_is_quoted_when_file_exists(self):
        path = Path("storage") / "test open target.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("ok", encoding="utf-8")
        try:
            command = f"start {path.resolve()}"
            normalized = _normalize_command(command)
        finally:
            path.unlink(missing_ok=True)

        if sys.platform == "win32":
            self.assertIn('start "" "', normalized)
        else:
            self.assertEqual(normalized, command)

    def test_terminal_strips_ansi_without_regex_dependency(self):
        self.assertEqual(_strip_ansi("\x1b[31mred\x1b[0m plain"), "red plain")

    def test_windows_terminal_uses_powershell_without_indicator_table(self):
        original_platform = terminal_mod.sys.platform
        try:
            terminal_mod.sys.platform = "win32"
            shell = _detect_shell("echo hello")
        finally:
            terminal_mod.sys.platform = original_platform

        self.assertEqual(shell[:3], ["powershell", "-NoProfile", "-Command"])
        self.assertEqual(shell[-1], "echo hello")

    def test_tool_arguments_must_be_semantically_grounded(self):
        self.assertFalse(
            _tool_call_grounded(
                "can you open it up for me",
                {"command": "start notepad"},
                [],
                "terminal",
            )
        )
        self.assertTrue(
            _tool_call_grounded(
                "open me chrome",
                {"command": "start chrome"},
                [],
                "terminal",
            )
        )
        self.assertTrue(
            _tool_call_grounded(
                "fetch me reddit threads",
                {"action": "front", "limit": "10", "sort": "relevance", "time": "week"},
                [],
                "reddit",
            )
        )

    def test_unknown_json_tool_name_is_not_fuzzy_mapped(self):
        schemas = [{"function": {"name": "terminal"}}]
        call, error = _try_parse_json_tool_call(
            '{"name":"terminal_extra","parameters":{"command":"start chrome"}}',
            schemas,
        )

        self.assertIsNone(call)
        self.assertIn("not an available tool", error)

    def test_maverick_text_tool_shape_parses_to_selected_tool(self):
        schemas = [self._schema("reddit")]
        call, error = _try_parse_json_tool_call(
            '{"type":"function","name":"reddit","parameters":{"action":"front","limit":5}}',
            schemas,
        )

        self.assertIsNone(error)
        self.assertEqual(call["name"], "reddit")
        self.assertEqual(json.loads(call["arguments"])["action"], "front")

    def test_json_tool_call_repairs_single_required_parameter_from_schema(self):
        schemas = [self._schema("load_tool")]
        call, error = _try_parse_json_tool_call(
            '{"name":"load_tool","parameters":{"tool_name":"playlist"}}',
            schemas,
        )

        self.assertIsNone(error)
        self.assertEqual(call["name"], "load_tool")
        self.assertEqual(json.loads(call["arguments"])["names"], "playlist")

    def test_schema_argument_repair_is_not_fuzzy_tool_mapping(self):
        repaired = _repair_schema_argument_names(
            "terminal",
            {"command": "echo ok", "surprise": "no"},
        )

        self.assertEqual(repaired, {"command": "echo ok", "surprise": "no"})

    def test_direct_markdown_hint_builds_forced_tool_call(self):
        schemas = [self._schema("reddit")]
        call = _forced_hint_tool_call(
            {"tool": "reddit", "args": {"action": "new"}, "direct": True},
            schemas,
        )

        self.assertEqual(call["name"], "reddit")
        self.assertEqual(json.loads(call["arguments"])["action"], "new")

    def test_unregistered_json_tool_shape_is_not_printed_raw(self):
        message = _json_tool_leak_message(
            '{"name":"fetch_latest_reddit_threads","parameters":{}}',
            [],
        )

        self.assertIn("fetch_latest_reddit_threads", message)
        self.assertNotIn('{"name"', message)

    def test_validator_rejects_unknown_parameters_before_dispatch(self):
        validator = ToolValidator()
        valid, error = validator.validate(
            "terminal",
            {"command": "echo ok", "surprise": "no"},
        )

        self.assertFalse(valid)
        self.assertIn("Unknown parameter", error)

    def test_validator_preserves_integer_coercion(self):
        validator = ToolValidator()
        args = {"path": "requirements.txt", "max_chars": "20"}
        valid, error = validator.validate("file_read", args)

        self.assertTrue(valid, error)
        self.assertEqual(args["max_chars"], 20)


class RegistryDispatchTests(unittest.TestCase):
    def test_execute_tool_legacy_string_contract_preserved(self):
        @tool(name="registry_test_echo", description="Registry test echo")
        def registry_test_echo(text: str) -> str:
            return text

        result = execute_tool("registry_test_echo", text="ok")
        unknown = execute_tool("registry_test_echo", text="ok", extra="no")

        self.assertEqual(result, "ok")
        self.assertEqual(
            unknown,
            "Error: 'registry_test_echo' received unknown parameter(s): extra. Accepted: text",
        )

    def test_execute_tool_structured_success_and_trace(self):
        @tool(name="registry_test_trace", description="Registry test trace")
        def registry_test_trace(text: str) -> str:
            return text.upper()

        stream = io.StringIO()
        with redirect_stdout(stream):
            result = execute_tool(
                "registry_test_trace",
                text="ok",
                response_format="structured",
                trace_enabled=True,
            )

        trace = json.loads(stream.getvalue().strip())
        self.assertEqual(result["result"]["output"], "OK")
        self.assertEqual(result["result"]["tool"], "registry_test_trace")
        self.assertEqual(result["meta"]["tool"], "registry_test_trace")
        self.assertEqual(result["meta"]["version"], "2.0.0")
        self.assertEqual(trace["event"], "TOOL TRACE")
        self.assertEqual(trace["status"], "SUCCESS")
        self.assertEqual(trace["schema_valid"], "YES")
        self.assertNotIn("ok", stream.getvalue())

    def test_execute_tool_structured_unknown_parameter_error(self):
        @tool(name="registry_test_unknown", description="Registry test unknown")
        def registry_test_unknown(text: str) -> str:
            return text

        result = execute_tool(
            "registry_test_unknown",
            text="ok",
            surprise="no",
            response_format="structured",
        )

        self.assertEqual(result["error"]["code"], "UNKNOWN_PARAMETER")
        self.assertEqual(result["error"]["field"], "parameters")
        self.assertFalse(result["error"]["retryable"])
        self.assertIn("suggestion", result["error"])

    def test_execute_tool_structured_timeout_error(self):
        @tool(name="registry_test_timeout", description="Registry test timeout")
        def registry_test_timeout() -> str:
            time.sleep(0.5)
            return "late"

        started = time.perf_counter()
        result = execute_tool(
            "registry_test_timeout",
            timeout_ms=10,
            response_format="structured",
        )
        duration = time.perf_counter() - started

        self.assertEqual(result["error"]["code"], "TOOL_TIMEOUT")
        self.assertTrue(result["error"]["retryable"])
        self.assertLess(duration, 0.45)

    def test_execute_tool_structured_exception_hides_raw_detail(self):
        @tool(name="registry_test_secret_error", description="Registry test secret error")
        def registry_test_secret_error() -> str:
            raise RuntimeError("secret-token-value")

        result = execute_tool(
            "registry_test_secret_error",
            response_format="structured",
        )

        self.assertEqual(result["error"]["code"], "TOOL_EXECUTION_ERROR")
        self.assertNotIn("secret-token-value", json.dumps(result))

    def test_validator_downstream_execution_still_receives_legacy_string(self):
        @tool(name="registry_test_validator", description="Registry test validator")
        def registry_test_validator(text: str) -> str:
            return f"seen {text}"

        validator = ToolValidator()
        ok, result = validator.validate_and_execute(
            "registry_test_validator",
            {"text": "value"},
        )

        self.assertTrue(ok)
        self.assertEqual(result, "seen value")


class MemoryRecallTests(unittest.TestCase):
    def test_memory_normalization_without_regex_import(self):
        self.assertEqual(_normalize_fact("User now lives in Delhi"), "User lives in Delhi")
        self.assertEqual(_normalize_fact("User actually lives in Mumbai"), "User lives in Mumbai")
        source = Path("memory") / "brain.py"
        text = source.read_text(encoding="utf-8")
        self.assertNotIn("import re", text)

    def test_recall_dedupes_before_winner_margin(self):
        original_embed = brain_mod.embed
        original_threshold = brain_mod.settings.memory_similarity_threshold
        original_margin = brain_mod.settings.memory_winner_margin

        brain = Brain.__new__(Brain)
        brain.memories = [
            {"text": "User name is Krish Verma", "importance": 0.5},
            {"text": "User name is Krish Verma", "importance": 0.5},
            {"text": "User lives in Delhi", "importance": 0.5},
        ]

        vectors = {
            "User name is Krish Verma": np.array([1.0, 0.0], dtype=np.float32),
            "User lives in Delhi": np.array([0.2, 0.8], dtype=np.float32),
        }

        def fake_embed(texts):
            if isinstance(texts, str):
                return vectors[texts]
            return np.array([vectors[text] for text in texts], dtype=np.float32)

        try:
            brain_mod.embed = fake_embed
            brain_mod.settings.memory_similarity_threshold = 0.1
            brain_mod.settings.memory_winner_margin = 0.3
            result = brain.recall("query", 5, np.array([1.0, 0.0], dtype=np.float32))
        finally:
            brain_mod.embed = original_embed
            brain_mod.settings.memory_similarity_threshold = original_threshold
            brain_mod.settings.memory_winner_margin = original_margin

        self.assertEqual(result, "User name is Krish Verma")


class RouterSelectionTests(unittest.TestCase):
    def test_routing_contrast_loads_from_markdown_without_literal_chat_phrase(self):
        contrast = _load_small_talk_text()

        self.assertIn("Routing Contrast Text", contrast)
        self.assertNotIn("how are you", contrast.lower())

    def test_category_gate_selects_single_exact_tool(self):
        router = ToolRouter(top_k=3)
        router.threshold = 0.2
        router._tool_names = ["strong", "near", "weak"]
        router._tool_embeddings = np.array(
            [
                [1.0, 0.0],
                [0.8, 0.2],
                [0.1, 0.9],
            ],
            dtype=np.float32,
        )
        router._categories = [{"name": "demo", "tools": ["strong", "near", "weak"], "texts": ["demo"]}]
        router._category_names = ["demo"]
        router._category_texts = ["demo"]
        router._category_owner_idx = [0]
        router._category_embeddings = np.array([[1.0, 0.0]], dtype=np.float32)

        original_tools = router_mod.get_tools
        original_get_tool = router_mod.get_tool
        try:
            router_mod.get_tools = lambda: [{"name": name} for name in router._tool_names]
            router_mod.get_tool = lambda name: {"name": name}
            selected = router.select_tools("long query", np.array([1.0, 0.0], dtype=np.float32))
        finally:
            router_mod.get_tools = original_tools
            router_mod.get_tool = original_get_tool

        self.assertEqual([tool["name"] for tool in selected], ["strong"])


class FileToolTests(unittest.TestCase):
    def test_file_write_read_append_and_list_metadata(self):
        path = Path("storage") / "file_tool_test.txt"
        path.unlink(missing_ok=True)
        try:
            created = file_write(str(path), "hello", mode="create_new")
            appended = file_write(str(path), "\nworld", mode="append")
            read = file_read(str(path), max_chars=1000)
            listed = file_list("storage", limit=200)
        finally:
            path.unlink(missing_ok=True)

        self.assertIn("Written to:", created)
        self.assertIn("Appended to:", appended)
        self.assertIn("Path:", read)
        self.assertIn("hello\nworld", read)
        self.assertIn("file_tool_test.txt", listed)

    def test_file_write_create_new_refuses_existing_file(self):
        path = Path("storage") / "file_tool_exists.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("first", encoding="utf-8")
        try:
            result = file_write(str(path), "second", mode="create_new")
            content = path.read_text(encoding="utf-8")
        finally:
            path.unlink(missing_ok=True)

        self.assertIn("File already exists", result)
        self.assertEqual(content, "first")

    def test_file_write_invalid_mode_has_no_parent_side_effect(self):
        root = Path("storage") / "invalid_mode_parent_test"
        path = root / "nested" / "file.txt"
        if root.exists():
            shutil.rmtree(root)

        result = file_write(str(path), "content", mode="bad_mode")

        self.assertIn("Invalid mode", result)
        self.assertFalse(root.exists())

    def test_file_write_overwrite_reports_existing_state(self):
        path = Path("storage") / "file_tool_overwrite.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("old", encoding="utf-8")
        try:
            result = file_write(str(path), "new", mode="overwrite")
            content = path.read_text(encoding="utf-8")
        finally:
            path.unlink(missing_ok=True)

        self.assertIn("Written to:", result)
        self.assertIn("Mode: overwrite", result)
        self.assertIn("Existed before: yes", result)
        self.assertEqual(content, "new")

    def test_file_list_file_path_returns_not_directory(self):
        path = Path("storage") / "file_list_file_target.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x", encoding="utf-8")
        try:
            result = file_list(str(path))
        finally:
            path.unlink(missing_ok=True)

        self.assertIn("Not a directory", result)


class TierOneToolUpgradeTests(unittest.TestCase):
    def test_terminal_structured_dry_run_emits_trace_without_running(self):
        marker = Path("storage") / "terminal_dry_run_marker.txt"
        marker.unlink(missing_ok=True)
        command = f'"{sys.executable}" -c "from pathlib import Path; Path({str(marker)!r}).write_text({("ran")!r})"'
        if sys.platform == "win32":
            command = f'& "{sys.executable}" -c "from pathlib import Path; Path({str(marker)!r}).write_text({("ran")!r})"'

        stream = io.StringIO()
        with redirect_stdout(stream):
            result = execute_tool(
                "terminal",
                command=command,
                dry_run=True,
                response_format="structured",
                trace_enabled=True,
            )

        trace = json.loads(stream.getvalue().strip())
        self.assertFalse(marker.exists())
        self.assertTrue(result["result"]["dry_run"])
        self.assertEqual(result["result"]["status"], "DRY_RUN")
        self.assertEqual(result["meta"]["version"], "2.0.0")
        self.assertEqual(trace["tool"], "terminal")
        self.assertEqual(trace["status"], "SUCCESS")

    def test_terminal_structured_timeout_is_typed(self):
        command = f'"{sys.executable}" -c "import time; time.sleep(0.3)"'
        if sys.platform == "win32":
            command = f'& "{sys.executable}" -c "import time; time.sleep(0.3)"'

        started = time.perf_counter()
        result = execute_tool(
            "terminal",
            command=command,
            timeout_ms=50,
            response_format="structured",
        )
        duration = time.perf_counter() - started

        self.assertEqual(result["error"]["code"], "COMMAND_TIMEOUT")
        self.assertTrue(result["error"]["retryable"])
        self.assertLess(duration, 1.2)

    def test_file_write_structured_create_new_via_registry(self):
        path = Path("storage") / "file_write_structured_test.txt"
        path.unlink(missing_ok=True)

        stream = io.StringIO()
        try:
            with redirect_stdout(stream):
                result = execute_tool(
                    "file_write",
                    path=str(path),
                    content="structured",
                    mode="create_new",
                    response_format="structured",
                    trace_enabled=True,
                )
            trace = json.loads(stream.getvalue().strip())
            content = path.read_text(encoding="utf-8")
        finally:
            path.unlink(missing_ok=True)

        self.assertEqual(content, "structured")
        self.assertEqual(result["result"]["mode"], "create_new")
        self.assertTrue(result["result"]["changed"])
        self.assertEqual(result["meta"]["version"], "2.0.0")
        self.assertEqual(trace["tool"], "file_write")
        self.assertEqual(trace["status"], "SUCCESS")

    def test_file_write_dry_run_does_not_create_parent(self):
        root = Path("storage") / "file_write_dry_run_parent"
        path = root / "nested" / "planned.txt"
        if root.exists():
            shutil.rmtree(root)

        result = file_write(
            str(path),
            "planned",
            dry_run=True,
            response_format="structured",
        )

        self.assertFalse(root.exists())
        self.assertTrue(result["result"]["dry_run"])
        self.assertFalse(result["result"]["changed"])

    def test_file_write_parent_creation_can_be_blocked(self):
        root = Path("storage") / "file_write_parent_blocked"
        path = root / "nested" / "blocked.txt"
        if root.exists():
            shutil.rmtree(root)

        result = file_write(
            str(path),
            "blocked",
            create_parent_dirs=False,
            response_format="structured",
        )

        self.assertEqual(result["error"]["code"], "PARENT_DIRECTORY_NOT_FOUND")
        self.assertFalse(result["error"]["retryable"])
        self.assertFalse(root.exists())


class NoteToolTests(unittest.TestCase):
    def test_note_delete_refuses_ambiguous_partial_match(self):
        original_path = notes_mod.NOTES_FILE
        path = Path("storage") / "notes_ambiguous_delete_test.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            '{"project alpha":{"content":"a","created":"x","updated":"x","tags":[]},"project beta":{"content":"b","created":"x","updated":"x","tags":[]}}',
            encoding="utf-8",
        )

        try:
            notes_mod.NOTES_FILE = path
            result = notes_mod.note_delete("project")
            data = path.read_text(encoding="utf-8")
        finally:
            notes_mod.NOTES_FILE = original_path
            path.unlink(missing_ok=True)

        self.assertIn("Ambiguous note title", result)
        self.assertIn("project alpha", data)
        self.assertIn("project beta", data)

    def test_note_update_allows_unique_partial_match(self):
        original_path = notes_mod.NOTES_FILE
        path = Path("storage") / "notes_unique_update_test.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            '{"project alpha":{"content":"a","created":"x","updated":"x","tags":[]},"meeting":{"content":"b","created":"x","updated":"x","tags":[]}}',
            encoding="utf-8",
        )

        try:
            notes_mod.NOTES_FILE = path
            result = notes_mod.note_update("alpha", content="changed")
            data = path.read_text(encoding="utf-8")
        finally:
            notes_mod.NOTES_FILE = original_path
            path.unlink(missing_ok=True)

        self.assertIn("Updated note 'project alpha'", result)
        self.assertIn("changed", data)


class YouTubeToolTests(unittest.TestCase):
    def test_playback_attempt_falls_back_to_opened_page_without_playing_claim(self):
        original_ffmpeg = youtube_mod._get_ffmpeg
        original_open = webbrowser.open
        opened = []

        try:
            youtube_mod._get_ffmpeg = lambda: None
            webbrowser.open = lambda url: opened.append(url) or True
            result = youtube_mod._start_playback_attempt(
                "https://example.com/video",
                "Grounded Song",
            )
        finally:
            youtube_mod._get_ffmpeg = original_ffmpeg
            webbrowser.open = original_open

        self.assertEqual(opened, ["https://example.com/video"])
        self.assertIn("Opened YouTube page", result)
        self.assertNotIn("Playing", result)

    def test_playlist_play_empty_playlist_returns_grounded_message(self):
        original_path = youtube_mod.PLAYLIST_PATH
        path = Path("storage") / "empty_playlist_test.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("[]", encoding="utf-8")

        try:
            youtube_mod.PLAYLIST_PATH = path
            result = youtube_mod.playlist_manage("play")
        finally:
            youtube_mod.PLAYLIST_PATH = original_path
            path.unlink(missing_ok=True)

        self.assertEqual(result, "No saved songs to play")


class DateTimeToolTests(unittest.TestCase):
    def test_city_timezone_name_resolves_from_timezone_database(self):
        result = datetime_info("Tokyo")

        self.assertNotIn("Unknown timezone", result)
        self.assertIn("Asia/Tokyo", result)

    def test_unknown_timezone_still_returns_clear_error(self):
        result = datetime_info("Not A Timezone")

        self.assertIn("Unknown timezone", result)


class ManifestAuditToolTests(unittest.TestCase):
    def test_tool_manifest_audit_reports_manifest_alignment(self):
        result = tool_manifest_audit(".", include_schema=True)

        self.assertIn("Status: success", result)
        self.assertIn("manifest_audit.py", result)
        self.assertIn("tool_manifest_audit", result)
        self.assertIn("Missing from manifest: none", result)
        self.assertIn("Missing from files: none", result)
        self.assertIn("Evidence: read-only local inspection; no files changed", result)

    def test_tool_manifest_audit_blocks_missing_root(self):
        missing = Path("storage") / "missing_manifest_audit_root"
        if missing.exists():
            shutil.rmtree(missing)

        result = tool_manifest_audit(str(missing))

        self.assertIn("Status: blocked", result)
        self.assertIn("root not found", result)


class ExternalRetryTests(unittest.TestCase):
    def test_hackernews_search_reports_bounded_provider_failure(self):
        original_get = hn_mod.httpx.get
        original_attempts = hn_mod.settings.external_request_attempts
        original_delay = hn_mod.settings.external_retry_delay
        calls = []

        def failing_get(*args, **kwargs):
            calls.append(args[0])
            raise hn_mod.httpx.TimeoutException("timeout")

        try:
            hn_mod.httpx.get = failing_get
            hn_mod.settings.external_request_attempts = 2
            hn_mod.settings.external_retry_delay = 0
            result = hn_mod.hackernews(action="search", query="ai", limit=2)
        finally:
            hn_mod.httpx.get = original_get
            hn_mod.settings.external_request_attempts = original_attempts
            hn_mod.settings.external_retry_delay = original_delay

        self.assertEqual(len(calls), 2)
        self.assertIn("Search unavailable: timeout after 2 attempt(s)", result)

    def test_web_fetch_reports_bounded_provider_failure(self):
        original_get = web_mod.httpx.get
        original_attempts = web_mod.settings.external_request_attempts
        original_delay = web_mod.settings.external_retry_delay
        calls = []

        def failing_get(*args, **kwargs):
            calls.append(args[0])
            raise web_mod.httpx.TimeoutException("timeout")

        try:
            web_mod.httpx.get = failing_get
            web_mod.settings.external_request_attempts = 2
            web_mod.settings.external_retry_delay = 0
            result = web_mod.web_fetch("https://example.com")
        finally:
            web_mod.httpx.get = original_get
            web_mod.settings.external_request_attempts = original_attempts
            web_mod.settings.external_retry_delay = original_delay

        self.assertEqual(len(calls), 2)
        self.assertIn("Error fetching page: timeout after 2 attempt(s)", result)

    def test_web_search_falls_back_from_tavily_to_ddgs(self):
        original_tavily = web_mod._tavily
        original_ddgs = web_mod.DDGS

        class FailingTavily:
            def search(self, *args, **kwargs):
                raise RuntimeError("down")

        class FakeDDGS:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def text(self, query, max_results):
                return [{"title": "Result", "body": "Body", "href": "https://example.com"}]

        try:
            web_mod._tavily = FailingTavily()
            web_mod.DDGS = FakeDDGS
            result = web_mod.web_search("query", max_results=1)
        finally:
            web_mod._tavily = original_tavily
            web_mod.DDGS = original_ddgs

        self.assertIn("Result", result)
        self.assertIn("https://example.com", result)


if __name__ == "__main__":
    unittest.main()
