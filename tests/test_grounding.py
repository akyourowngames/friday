import io
import json
import shutil
import sys
import tempfile
import time
import unittest
import webbrowser
from contextlib import redirect_stdout
from datetime import date, datetime
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.core import (
    _build_tool_answer_instruction,
    _forced_contextual_tool_call,
    _has_backtick_tool_call,
    _local_time_context,
    _load_tool_policy,
    _maybe_reuse_latest_context_tool,
    _prepare_tool_args_for_answer,
    _repair_contextual_tool_args,
    _repair_contextual_tool_call,
    _repair_search_query_specificity,
    _should_keep_memory_context,
    _should_suppress_memory_context,
    _should_use_profile_context,
    _should_use_memory_context,
    _tool_call_grounded,
    _tool_result_content,
    _try_parse_json_tool_call,
)
from agent.validator import ToolValidator
from memory.brain import Brain
import memory.brain as brain_mod
from memory.brain import _normalize_fact
from tools.terminal import _detect_shell, _normalize_command, _normalize_launch_target, _strip_ansi
import tools.terminal as terminal_mod
from tools.registry import execute_tool, get_tool_schemas, get_tools, tool
from tools.files import file_read, file_write, file_list
from tools.datetime_tool import datetime_info
from tools.manifest_audit import tool_manifest_audit
import tools.youtube as youtube_mod
import tools.hackernews as hn_mod
import tools.reddit as reddit_mod
import tools.web as web_mod
import tools.notes as notes_mod
import tools.browser as browser_mod
import tools.navigator as navigator_mod
import api_server as api_mod
from agent.router import ToolRouter, _load_small_talk_text
import agent.router as router_mod
import agent.core as core_mod


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

    def test_router_keeps_small_talk_out_of_weak_tool_path(self):
        original_get_tools = router_mod.get_tools
        try:
            router = ToolRouter()
            router_mod.get_tools = lambda: [{"name": "note_read"}]
            router.threshold = 0.16
            router.winner_margin = 0.15
            router._tool_names = ["note_read"]
            router._tool_texts = ["note_read: read saved notes"]
            router._tool_embeddings = np.array([[0.20]], dtype=np.float32)
            router._small_talk_emb = np.array([0.20], dtype=np.float32)

            selected = router.select_tools("how are you", q_emb=np.array([1.0], dtype=np.float32))
        finally:
            router_mod.get_tools = original_get_tools

        self.assertEqual(selected, [])
        self.assertEqual(router.last_decision()["reason"], "small_talk_contrast_won")

    def test_router_still_selects_strong_action_tool(self):
        original_get_tools = router_mod.get_tools
        original_get_tool = router_mod.get_tool
        try:
            router_mod.get_tools = lambda: [{"name": "youtube_play"}]
            router_mod.get_tool = lambda name: {"name": name}
            router = ToolRouter()
            router.threshold = 0.16
            router.winner_margin = 0.15
            router._tool_names = ["youtube_play"]
            router._tool_texts = ["youtube_play: open or play YouTube"]
            router._tool_embeddings = np.array([[0.50]], dtype=np.float32)
            router._small_talk_emb = np.array([0.05], dtype=np.float32)

            selected = router.select_tools("open youtube", q_emb=np.array([1.0], dtype=np.float32))
        finally:
            router_mod.get_tools = original_get_tools
            router_mod.get_tool = original_get_tool

        self.assertEqual([tool["name"] for tool in selected], ["youtube_play"])
        self.assertEqual(router.last_decision()["reason"], "selected")

    def test_graph_memory_survives_router_small_talk_reason(self):
        context = "Graph memory: User crush ankita"
        decision = {"reason": "small_talk_contrast_won"}

        keep = _should_keep_memory_context(
            context,
            "who is my crush huh",
            np.array([1.0], dtype=np.float32),
            [],
            decision,
        )

        self.assertTrue(keep)

    def test_small_talk_does_not_keep_text_only_memory(self):
        original = core_mod._should_use_memory_context
        try:
            core_mod._should_use_memory_context = lambda user_input, q_emb, selected_tools: False
            keep = core_mod._should_keep_memory_context(
                "Text memory: my cursh is ankita",
                "how are you",
                np.array([1.0], dtype=np.float32),
                [],
                {"reason": "small_talk_contrast_won"},
            )
        finally:
            core_mod._should_use_memory_context = original

        self.assertFalse(keep)

    def test_broad_memory_followup_uses_profile_context(self):
        original_embed = core_mod.embed
        original_followup = core_mod._looks_like_context_followup

        def fake_embed(texts, normalize=True):
            if isinstance(texts, list):
                return np.array([[1.0, 0.0], [0.0, 1.0]], dtype=np.float32)
            return np.array([1.0, 0.0], dtype=np.float32)

        try:
            core_mod.embed = fake_embed
            core_mod._looks_like_context_followup = lambda text: True
            self.assertTrue(_should_use_profile_context("what do you know about me", np.array([1.0, 0.0], dtype=np.float32)))
            self.assertFalse(_should_use_profile_context("where do I live", np.array([0.0, 1.0], dtype=np.float32)))
            self.assertTrue(_should_use_profile_context("anything else", np.array([0.0, 1.0], dtype=np.float32), last_profile_context=True))
        finally:
            core_mod.embed = original_embed
            core_mod._looks_like_context_followup = original_followup

    def test_local_time_context_names_afternoon_explicitly(self):
        context = _local_time_context(datetime(2026, 5, 20, 16, 15))

        self.assertIn("Current local time of day: afternoon", context)
        self.assertIn("do not", context.lower())

    def test_structured_tool_result_is_json_for_final_answer_context(self):
        content = _tool_result_content({
            "result": {
                "title": "DeepSeek AI",
                "url": "https://example.com",
            }
        })

        parsed = json.loads(content)
        self.assertEqual(parsed["result"]["title"], "DeepSeek AI")
        self.assertNotIn("'result'", content)

    def test_tool_answer_instruction_blocks_raw_payload_leak(self):
        instruction = _build_tool_answer_instruction(
            "fetch latest DeepSeek model details",
            ["web_fetch"],
        )

        self.assertIn("answer the user in natural language", instruction)
        self.assertIn("Do not expose raw JSON", instruction)
        self.assertIn("include the useful observed titles", instruction)
        self.assertIn("result has a query field", instruction)
        self.assertIn("User request: fetch latest DeepSeek model details", instruction)

    def test_tool_args_request_structured_context_when_supported(self):
        prepared = _prepare_tool_args_for_answer(
            "web_fetch",
            {"url": "https://example.com"},
        )

        self.assertEqual(prepared["response_format"], "structured")
        self.assertEqual(prepared["url"], "https://example.com")

    def test_tool_args_preserve_explicit_response_format(self):
        prepared = _prepare_tool_args_for_answer(
            "web_fetch",
            {"url": "https://example.com", "response_format": "legacy"},
        )

        self.assertEqual(prepared["response_format"], "legacy")

    def test_backtick_shell_command_is_tool_leak_when_terminal_available(self):
        schemas = [self._schema("terminal")]
        self.assertTrue(_has_backtick_tool_call("`start image.png`", schemas))

    def test_backtick_plain_text_is_not_tool_leak_without_terminal(self):
        schemas = [{"function": {"name": "web_search"}}]
        self.assertFalse(_has_backtick_tool_call("Use `hello` as the title.", schemas))

    def test_backtick_plain_text_is_not_tool_leak_with_terminal_available(self):
        schemas = [self._schema("terminal")]
        self.assertFalse(_has_backtick_tool_call("Use `hello` as the title.", schemas))

    def test_backtick_tool_name_reference_is_not_leak(self):
        schemas = [self._schema("web_search")]
        self.assertFalse(_has_backtick_tool_call("Use the `web_search` tool.", schemas))

    def test_backtick_parameter_name_reference_is_not_leak(self):
        schemas = [self._schema("web_search")]
        self.assertFalse(_has_backtick_tool_call("Specify the `query` here.", schemas))

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

    def test_windows_start_terminal_uses_configured_launch_target(self):
        original_platform = terminal_mod.sys.platform
        original_which = terminal_mod.shutil.which
        try:
            terminal_mod.sys.platform = "win32"
            terminal_mod.shutil.which = lambda candidate: candidate if candidate == "cmd.exe" else None
            normalized = _normalize_launch_target("start terminal")
        finally:
            terminal_mod.sys.platform = original_platform
            terminal_mod.shutil.which = original_which

        self.assertEqual(normalized, "Start-Process 'cmd.exe'")

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
                "open me notepad",
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

    def test_json_tool_call_text_for_hackernews_is_detected(self):
        schemas = [self._schema("hackernews")]
        call, error = _try_parse_json_tool_call(
            '{"name":"hackernews","parameters":{"action":"show_item","id":"48225297","response_format":"structured"}}',
            schemas,
        )

        self.assertIsNone(error)
        self.assertEqual(call["name"], "hackernews")
        self.assertEqual(json.loads(call["arguments"])["action"], "show_item")

    def test_hackernews_show_item_alias_maps_to_comments(self):
        original_fetch = hn_mod._fetch_item_detail_result
        calls = []

        def fake_fetch(item_id, timeout_seconds=15.0, stats=None):
            calls.append(item_id)
            return hn_mod._operation_result(
                "comments",
                "Story detail",
                [{"id": item_id, "title": "Project Hail Mary"}],
                extra={"query": item_id, "comments": [], "comment_count": 0},
            )

        try:
            hn_mod._fetch_item_detail_result = fake_fetch
            result = hn_mod.hackernews(
                action="show_item",
                id="48225297",
                response_format="structured",
            )
        finally:
            hn_mod._fetch_item_detail_result = original_fetch

        self.assertEqual(calls, ["48225297"])
        self.assertEqual(result["result"]["action"], "comments")
        self.assertEqual(result["result"]["items"][0]["title"], "Project Hail Mary")

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

    def test_file_tools_return_structured_evidence_when_requested(self):
        read_result = file_read("requirements.txt", response_format="structured")
        list_result = file_list(".", limit=5, response_format="structured")

        self.assertEqual(read_result["meta"]["tool"], "file_read")
        self.assertTrue(read_result["result"]["readable"])
        self.assertIn("path", read_result["result"])
        self.assertEqual(list_result["meta"]["tool"], "file_list")
        self.assertGreaterEqual(list_result["result"]["count"], 1)
        self.assertIn("items", list_result["result"])

    def test_router_exposes_grounded_decision_reason(self):
        router = ToolRouter()

        selected = router.select_tools("hi")
        decision = router.last_decision()

        self.assertEqual(selected, [])
        self.assertEqual(decision["reason"], "below_embedding_min_chars")

    def test_context_followup_reuses_latest_information_tool(self):
        original = core_mod._looks_like_context_followup
        core_mod._looks_like_context_followup = lambda text: "more" in text.lower() or "return" in text.lower()
        messages = [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "function": {
                            "name": "web_search",
                            "arguments": '{"query":"google models"}',
                        }
                    }
                ],
            },
            {"role": "tool", "content": '{"result":{"query":"google models"}}'},
            {"role": "assistant", "content": "One result found."},
        ]
        selected_tool = None
        for tool_info in get_tools():
            if tool_info["name"] == "reddit":
                selected_tool = tool_info
                break
        self.assertIsNotNone(selected_tool)

        try:
            reused = _maybe_reuse_latest_context_tool("return me more", [selected_tool], messages)
        finally:
            core_mod._looks_like_context_followup = original

        self.assertEqual(reused[0]["name"], "web_search")

    def test_context_followup_reuses_latest_tool_when_router_selected_none(self):
        original = core_mod._looks_like_context_followup
        core_mod._looks_like_context_followup = lambda text: True
        messages = [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "function": {
                            "name": "hackernews",
                            "arguments": '{"action":"search","query":"project hail mary"}',
                        }
                    }
                ],
            },
            {"role": "tool", "content": '{"meta":{"tool":"hackernews"},"result":{"items":[{"id":"48225297"}]}}'},
            {"role": "assistant", "content": "HN result found."},
        ]

        try:
            reused = _maybe_reuse_latest_context_tool("can you go deep dive into it", [], messages)
        finally:
            core_mod._looks_like_context_followup = original

        self.assertEqual(reused[0]["name"], "hackernews")

    def test_context_followup_repairs_web_query_before_dispatch(self):
        original = core_mod._looks_like_context_followup
        core_mod._looks_like_context_followup = lambda text: text in {"return me more", "more"}
        messages = [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "function": {
                            "name": "web_search",
                            "arguments": '{"query":"models from google","max_results":1}',
                        }
                    }
                ],
            },
            {"role": "tool", "content": '{"result":{"query":"models from google"}}'},
            {"role": "assistant", "content": "One result found."},
        ]

        try:
            repaired = _repair_contextual_tool_args(
                "web_search",
                {"query": "more", "max_results": 1},
                "return me more",
                messages,
            )
        finally:
            core_mod._looks_like_context_followup = original

        self.assertEqual(repaired["query"], "models from google")
        self.assertEqual(repaired["max_results"], 8)

    def test_context_followup_deepens_thin_web_search_to_fetch(self):
        original = core_mod._looks_like_context_followup
        core_mod._looks_like_context_followup = lambda text: text == "return me more"
        messages = [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "function": {
                            "name": "web_search",
                            "arguments": '{"query":"models from google","max_results":10}',
                        }
                    }
                ],
            },
            {
                "role": "tool",
                "content": json.dumps(
                    {
                        "result": {
                            "result_count": 1,
                            "results": [
                                {
                                    "title": "Google models",
                                    "url": "https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/google-models",
                                }
                            ],
                        },
                        "meta": {"tool": "web_search"},
                    }
                ),
            },
            {"role": "assistant", "content": "One result found."},
        ]

        try:
            name, args = _repair_contextual_tool_call(
                "web_search",
                {"query": "more", "max_results": 10},
                "return me more",
                messages,
            )
        finally:
            core_mod._looks_like_context_followup = original

        self.assertEqual(name, "web_fetch")
        self.assertEqual(args["url"], "https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/google-models")

    def test_context_followup_hackernews_deep_dive_fetches_item_comments(self):
        original = core_mod._looks_like_context_followup
        core_mod._looks_like_context_followup = lambda text: True
        messages = [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "function": {
                            "name": "hackernews",
                            "arguments": '{"action":"search","query":"project hail mary"}',
                        }
                    }
                ],
            },
            {
                "role": "tool",
                "content": json.dumps(
                    {
                        "meta": {"tool": "hackernews"},
                        "result": {
                            "action": "search",
                            "items": [
                                {
                                    "id": "48225297",
                                    "title": "Project Hail Mary - Stellar Navigation Chart",
                                }
                            ],
                        },
                    }
                ),
            },
            {"role": "assistant", "content": "HN result found."},
        ]

        try:
            name, args = _repair_contextual_tool_call(
                "hackernews",
                {"action": "search", "query": "deep dive into it"},
                "can you go deep dive into it",
                messages,
            )
        finally:
            core_mod._looks_like_context_followup = original

        self.assertEqual(name, "hackernews")
        self.assertEqual(args["action"], "comments")
        self.assertEqual(args["query"], "48225297")

    def test_context_followup_forces_repaired_hackernews_tool_call(self):
        original = core_mod._looks_like_context_followup
        core_mod._looks_like_context_followup = lambda text: True
        messages = [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "function": {
                            "name": "hackernews",
                            "arguments": '{"action":"search","query":"project hail mary"}',
                        }
                    }
                ],
            },
            {
                "role": "tool",
                "content": json.dumps(
                    {
                        "meta": {"tool": "hackernews"},
                        "result": {"items": [{"id": "48225297"}]},
                    }
                ),
            },
        ]

        try:
            call = _forced_contextual_tool_call(
                "can you go deep dive into it",
                [self._schema("hackernews")],
                messages,
            )
        finally:
            core_mod._looks_like_context_followup = original

        self.assertEqual(call["name"], "hackernews")
        self.assertEqual(json.loads(call["arguments"])["action"], "comments")
        self.assertEqual(json.loads(call["arguments"])["query"], "48225297")

    def test_web_search_query_specificity_preserves_user_terms(self):
        repaired = _repair_search_query_specificity(
            "web_search",
            {"query": "models", "max_results": 10},
            "fetch me latest details on models from google",
        )

        self.assertEqual(repaired["query"], "fetch me latest details on models from google")

    def test_small_talk_router_decision_suppresses_memory_context(self):
        self.assertTrue(_should_suppress_memory_context({"reason": "small_talk_contrast_won"}))
        self.assertFalse(_should_suppress_memory_context({"reason": "selected"}))

    def test_memory_context_requires_memory_intent_without_tools(self):
        original_embed = core_mod.embed
        vectors = {
            "who am i": np.array([1.0, 0.0], dtype=np.float32),
            "how are you man": np.array([0.0, 1.0], dtype=np.float32),
        }

        def fake_embed(texts):
            if isinstance(texts, str):
                return vectors[texts]
            return np.array(
                [
                    np.array([1.0, 0.0], dtype=np.float32),
                    np.array([0.0, 1.0], dtype=np.float32),
                ],
                dtype=np.float32,
            )

        try:
            core_mod.embed = fake_embed
            self.assertTrue(_should_use_memory_context("who am i", vectors["who am i"], []))
            self.assertFalse(_should_use_memory_context("how are you man", vectors["how are you man"], []))
        finally:
            core_mod.embed = original_embed


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
            time.sleep(0.2)
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
        self.assertLess(duration, 0.30)

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
            brain._embeddings = np.array(
                [vectors[memory["text"]] for memory in brain.memories],
                dtype=np.float32,
            )
            brain._rebuild_index = lambda: None
            result = brain.recall("query", 5, np.array([1.0, 0.0], dtype=np.float32))
        finally:
            brain_mod.embed = original_embed
            brain_mod.settings.memory_similarity_threshold = original_threshold
            brain_mod.settings.memory_winner_margin = original_margin

        self.assertEqual(result, "User name is Krish Verma")

    def test_brain_builds_persistent_index_and_assessment_reports_full_coverage(self):
        original_memory_dir = brain_mod.settings.memory_dir
        original_backup_dir = brain_mod.settings.memory_backup_dir
        original_index_file = brain_mod.settings.memory_index_file
        original_embeddings_file = brain_mod.settings.memory_embeddings_file
        original_archive_file = brain_mod.settings.memory_archive_file
        original_limit = brain_mod.settings.memory_max_entries
        original_embed = brain_mod.embed

        with tempfile.TemporaryDirectory() as tmp:
            memory_dir = Path(tmp) / "memories"
            backup_dir = Path(tmp) / "backups"
            memory_dir.mkdir(parents=True, exist_ok=True)
            payload = [
                {"text": "User lives in Delhi", "importance": 0.6, "ts": "10:00:00"},
                {"text": "User likes chess puzzles", "importance": 0.4, "ts": "10:05:00"},
            ]
            (memory_dir / f"memory_{date.today().isoformat()}.json").write_text(
                json.dumps(payload, indent=2),
                encoding="utf-8",
            )

            def fake_embed(texts, normalize=True):
                mapping = {
                    "User lives in Delhi": np.array([1.0, 0.0], dtype=np.float32),
                    "User likes chess puzzles": np.array([0.5, 0.5], dtype=np.float32),
                    "where does the user live": np.array([1.0, 0.0], dtype=np.float32),
                }
                if isinstance(texts, str):
                    return mapping[texts]
                return np.array([mapping[text] for text in texts], dtype=np.float32)

            try:
                brain_mod.settings.memory_dir = str(memory_dir)
                brain_mod.settings.memory_backup_dir = str(backup_dir)
                brain_mod.settings.memory_index_file = "memory_index.json"
                brain_mod.settings.memory_embeddings_file = "memory_embeddings.npy"
                brain_mod.settings.memory_archive_file = "memory_archive.jsonl"
                brain_mod.settings.memory_max_entries = 10
                brain_mod.MEMORY_DIR = Path(brain_mod.settings.memory_dir)
                brain_mod.BACKUP_DIR = Path(brain_mod.settings.memory_backup_dir)
                brain_mod.embed = fake_embed

                brain = Brain()
                assessment = brain.system_assessment()
                recalled = brain.recall("where does the user live")
                index_exists = (memory_dir / "memory_index.json").exists()
                embeddings_exists = (memory_dir / "memory_embeddings.npy").exists()
            finally:
                brain_mod.settings.memory_dir = original_memory_dir
                brain_mod.settings.memory_backup_dir = original_backup_dir
                brain_mod.settings.memory_index_file = original_index_file
                brain_mod.settings.memory_embeddings_file = original_embeddings_file
                brain_mod.settings.memory_archive_file = original_archive_file
                brain_mod.settings.memory_max_entries = original_limit
                brain_mod.MEMORY_DIR = Path(brain_mod.settings.memory_dir)
                brain_mod.BACKUP_DIR = Path(brain_mod.settings.memory_backup_dir)
                brain_mod.embed = original_embed

        self.assertTrue(index_exists)
        self.assertTrue(embeddings_exists)
        self.assertEqual(assessment["entry_count"], 2)
        self.assertEqual(assessment["indexed_count"], 2)
        self.assertEqual(assessment["index_coverage_ratio"], 1.0)
        self.assertEqual(recalled, "User lives in Delhi")

    def test_commit_capacity_trim_archives_old_entries(self):
        original_memory_dir = brain_mod.settings.memory_dir
        original_backup_dir = brain_mod.settings.memory_backup_dir
        original_index_file = brain_mod.settings.memory_index_file
        original_embeddings_file = brain_mod.settings.memory_embeddings_file
        original_archive_file = brain_mod.settings.memory_archive_file
        original_limit = brain_mod.settings.memory_max_entries
        original_embed = brain_mod.embed

        with tempfile.TemporaryDirectory() as tmp:
            memory_dir = Path(tmp) / "memories"
            backup_dir = Path(tmp) / "backups"
            memory_dir.mkdir(parents=True, exist_ok=True)

            def fake_embed(texts, normalize=True):
                if isinstance(texts, str):
                    return np.array([1.0, 0.0], dtype=np.float32)
                return np.array([[1.0, 0.0] for _ in texts], dtype=np.float32)

            try:
                brain_mod.settings.memory_dir = str(memory_dir)
                brain_mod.settings.memory_backup_dir = str(backup_dir)
                brain_mod.settings.memory_index_file = "memory_index.json"
                brain_mod.settings.memory_embeddings_file = "memory_embeddings.npy"
                brain_mod.settings.memory_archive_file = "memory_archive.jsonl"
                brain_mod.settings.memory_max_entries = 2
                brain_mod.MEMORY_DIR = Path(brain_mod.settings.memory_dir)
                brain_mod.BACKUP_DIR = Path(brain_mod.settings.memory_backup_dir)
                brain_mod.embed = fake_embed

                brain = Brain()
                self.assertTrue(brain.commit("User likes black coffee", importance=0.1))
                self.assertTrue(brain.commit("User lives in Delhi", importance=0.5))
                self.assertTrue(brain.commit("User works remotely from home", importance=0.9))
            finally:
                brain_mod.settings.memory_dir = original_memory_dir
                brain_mod.settings.memory_backup_dir = original_backup_dir
                brain_mod.settings.memory_index_file = original_index_file
                brain_mod.settings.memory_embeddings_file = original_embeddings_file
                brain_mod.settings.memory_archive_file = original_archive_file
                brain_mod.settings.memory_max_entries = original_limit
                brain_mod.MEMORY_DIR = Path(brain_mod.settings.memory_dir)
                brain_mod.BACKUP_DIR = Path(brain_mod.settings.memory_backup_dir)
                brain_mod.embed = original_embed

            archive_text = (memory_dir / "memory_archive.jsonl").read_text(encoding="utf-8")
            remaining = [item["text"] for item in brain.memories]

        self.assertEqual(len(remaining), 2)
        self.assertNotIn("User likes black coffee", remaining)
        self.assertIn('"reason": "capacity"', archive_text)

    def test_benchmark_recall_reports_average_latency(self):
        original_embed = brain_mod.embed

        brain = Brain.__new__(Brain)
        brain.memories = [{"text": "User lives in Delhi", "importance": 0.5}]
        brain._embeddings = np.array([[1.0, 0.0]], dtype=np.float32)
        brain._rebuild_index = lambda: None

        vectors = {"where does the user live": np.array([1.0, 0.0], dtype=np.float32)}

        def fake_embed(texts, normalize=True):
            if isinstance(texts, str):
                return vectors[texts]
            return np.array([vectors[text] for text in texts], dtype=np.float32)

        try:
            brain_mod.embed = fake_embed
            report = brain.benchmark_recall("where does the user live", runs=3, k=1)
        finally:
            brain_mod.embed = original_embed

        self.assertEqual(report["runs"], 3)
        self.assertEqual(report["result_count"], 1)
        self.assertGreaterEqual(report["avg_ms"], 0.0)


class MemoryExtractionContextTests(unittest.TestCase):
    def test_extract_and_store_passes_recent_context_for_followup_storage(self):
        agent = core_mod.Agent.__new__(core_mod.Agent)
        committed = []
        observed = {}

        class FakeBrain:
            def commit(self, fact):
                committed.append(fact)
                return True

        class FakeLLM:
            def extract_facts(self, user_input, assistant_response, recent_user_context=""):
                observed["user_input"] = user_input
                observed["recent_user_context"] = recent_user_context
                if "class 11th" in recent_user_context and "Ankita" in recent_user_context:
                    return ["Ankita is in class 11th"]
                return []

        agent.brain = FakeBrain()
        agent.llm = FakeLLM()

        agent._extract_and_store(
            "i am telling you to store this",
            "Sir, I have stored that.",
            "user: so she is in class 11th right now bud\nassistant: Sir, Ankita is your crush.",
        )

        self.assertEqual(observed["user_input"], "i am telling you to store this")
        self.assertEqual(committed, ["Ankita is in class 11th"])


class RouterSelectionTests(unittest.TestCase):
    def test_routing_contrast_loads_from_markdown_without_literal_chat_phrase(self):
        contrast = _load_small_talk_text()

        self.assertIn("Routing Contrast Text", contrast)
        self.assertNotIn("how are you", contrast.lower())

    def test_relative_floor_removes_weak_tail_candidates(self):
        router = ToolRouter(top_k=3)
        router.threshold = 0.1
        router.winner_margin = 0.0
        router._small_talk_emb = np.array([0.0, 0.0], dtype=np.float32)
        router._tool_names = ["strong", "near", "weak"]
        router._tool_embeddings = np.array(
            [
                [1.0, 0.0],
                [0.8, 0.2],
                [0.1, 0.9],
            ],
            dtype=np.float32,
        )

        original_tools = router_mod.get_tools
        original_get_tool = router_mod.get_tool
        original_floor = router_mod.settings.tool_relative_floor
        try:
            router_mod.get_tools = lambda: [{"name": name} for name in router._tool_names]
            router_mod.get_tool = lambda name: {"name": name}
            router_mod.settings.tool_relative_floor = 0.72
            selected = router.select_tools("long query", np.array([1.0, 0.0], dtype=np.float32))
        finally:
            router_mod.get_tools = original_tools
            router_mod.get_tool = original_get_tool
            router_mod.settings.tool_relative_floor = original_floor

        self.assertEqual([tool["name"] for tool in selected], ["strong", "near"])


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


class BrowserAutomationToolTests(unittest.TestCase):
    def test_browser_target_config_parses_markdown_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "targets.md"
            config_path.write_text(
                "\n".join(
                    [
                        "# Targets",
                        "## social_profile",
                        "url: https://example.com/profile",
                        "wait_until: domcontentloaded",
                        "field: followers | source: meta | label: followers",
                        "field: profile_title | source: title",
                    ]
                ),
                encoding="utf-8",
            )

            targets, error = browser_mod._load_targets(str(config_path))

        self.assertIsNone(error)
        self.assertEqual(targets["social_profile"]["url"], "https://example.com/profile")
        self.assertEqual(targets["social_profile"]["fields"][0]["name"], "followers")
        self.assertEqual(targets["social_profile"]["fields"][0]["label"], "followers")

    def test_browser_extract_uses_markdown_configured_fields(self):
        original_load_page = browser_mod._load_page

        def fake_load_page(url, engine, timeout_ms, wait_until, max_text_chars, fields, storage_state="", read_mode="fields", dom_policy=None):
            return {
                "requested_url": url,
                "final_url": "https://example.com/profile",
                "status_code": 200,
                "title": "Example Profile",
                "text": "Example visible body",
                "text_truncated": False,
                "meta": [
                    {
                        "name": "description",
                        "property": "",
                        "content": "1,234 followers, 56 following, 7 posts",
                    }
                ],
                "selector_values": {},
                "engine_used": "playwright",
                "degraded": False,
                "degraded_reason": "",
            }, None

        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "targets.md"
            config_path.write_text(
                "\n".join(
                    [
                        "# Targets",
                        "## social_profile",
                        "url: https://example.com/profile",
                        "field: followers | source: meta | label: followers",
                    ]
                ),
                encoding="utf-8",
            )
            try:
                browser_mod._load_page = fake_load_page
                result = browser_mod.browser_extract(
                    target="social_profile",
                    config_path=str(config_path),
                    response_format="structured",
                )
            finally:
                browser_mod._load_page = original_load_page

        self.assertEqual(result["meta"]["tool"], "browser_extract")
        self.assertEqual(result["result"]["engine_used"], "playwright")
        self.assertEqual(result["result"]["matched_count"], 1)
        self.assertEqual(result["result"]["fields"][0]["value"], "1,234")

    def test_browser_extract_reuses_markdown_storage_state(self):
        original_load_page = browser_mod._load_page
        observed = {}

        def fake_load_page(url, engine, timeout_ms, wait_until, max_text_chars, fields, storage_state="", read_mode="fields", dom_policy=None):
            observed["storage_state"] = storage_state
            return {
                "requested_url": url,
                "final_url": "https://example.com/private",
                "status_code": 200,
                "title": "Private Page",
                "text": "42 followers",
                "text_truncated": False,
                "meta": [],
                "selector_values": {},
                "engine_used": "playwright",
                "degraded": False,
                "degraded_reason": "",
                "storage_state_used": bool(storage_state),
            }, None

        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            state_path.write_text("{}", encoding="utf-8")
            config_path = Path(tmp) / "targets.md"
            config_path.write_text(
                "\n".join(
                    [
                        "# Targets",
                        "## private_profile",
                        "url: https://example.com/private",
                        f"storage_state: {state_path}",
                        "field: followers | source: text | label: followers",
                    ]
                ),
                encoding="utf-8",
            )
            try:
                browser_mod._load_page = fake_load_page
                result = browser_mod.browser_extract(
                    target="private_profile",
                    config_path=str(config_path),
                    response_format="structured",
                )
            finally:
                browser_mod._load_page = original_load_page

        self.assertEqual(observed["storage_state"], str(state_path.resolve()))
        self.assertTrue(result["result"]["storage_state_used"])
        self.assertEqual(result["result"]["storage_state_path"], str(state_path.resolve()))

    def test_browser_extract_direct_url_validates_url(self):
        result = browser_mod.browser_extract(
            url="example.com/profile",
            response_format="structured",
        )

        self.assertEqual(result["error"]["code"], "INVALID_URL")

    def test_browser_login_session_schema_is_registered(self):
        schema = None
        for candidate in get_tool_schemas():
            if candidate["function"]["name"] == "browser_login_session":
                schema = candidate
                break
        self.assertIsNotNone(schema)
        props = schema["function"]["parameters"]["properties"]

        self.assertIn("storage_state", props)
        self.assertIn("timeout_ms", props)

    def test_browser_login_session_errors_use_login_tool_meta(self):
        result = browser_mod.browser_login_session(
            url="example.com/login",
            response_format="structured",
        )

        self.assertEqual(result["meta"]["tool"], "browser_login_session")
        self.assertEqual(result["error"]["code"], "INVALID_URL")


class NavigatorToolTests(unittest.TestCase):
    def _schema(self, name):
        for schema in get_tool_schemas():
            if schema["function"]["name"] == name:
                return schema
        raise AssertionError(f"schema not found: {name}")

    def test_navigator_schema_is_registered(self):
        schema = self._schema("navigator")
        props = schema["function"]["parameters"]["properties"]

        self.assertIn("origin", props)
        self.assertIn("destination", props)
        self.assertIn("mode", props)
        self.assertIn("response_format", props)

    def test_navigator_structured_route_uses_open_provider_fields(self):
        original_get = navigator_mod.httpx.get
        calls = []

        class FakeResponse:
            def __init__(self, data):
                self._data = data

            def raise_for_status(self):
                return None

            def json(self):
                return self._data

        def fake_get(url, params=None, timeout=None, headers=None):
            calls.append({"url": url, "params": params, "timeout": timeout, "headers": headers})
            if url == navigator_mod.settings.navigator_geocode_url:
                query = params.get("q")
                if query == "Origin City":
                    return FakeResponse([
                        {
                            "name": "Origin City",
                            "display_name": "Origin City, Test State",
                            "lat": "28.6100",
                            "lon": "77.2300",
                        }
                    ])
                return FakeResponse([
                    {
                        "name": "Destination City",
                        "display_name": "Destination City, Test State",
                        "lat": "26.9200",
                        "lon": "75.7900",
                    }
                ])
            return FakeResponse(
                {
                    "code": "Ok",
                    "routes": [
                        {
                            "distance": 280500,
                            "duration": 13500,
                            "geometry": "encoded-polyline",
                        }
                    ],
                }
            )

        try:
            navigator_mod.httpx.get = fake_get
            result = navigator_mod.navigator(
                "Origin City",
                "Destination City",
                timeout_ms=5000,
                response_format="structured",
            )
        finally:
            navigator_mod.httpx.get = original_get

        self.assertEqual(len(calls), 3)
        self.assertEqual(calls[0]["timeout"], 5.0)
        self.assertEqual(result["meta"]["tool"], "navigator")
        self.assertEqual(result["result"]["route"]["distance_km"], 280.5)
        self.assertEqual(result["result"]["route"]["provider"], "osrm")
        self.assertFalse(result["result"]["route"]["fallback_used"])
        self.assertEqual(result["result"]["origin"]["source"], "nominatim")

    def test_navigator_falls_back_to_straight_line_when_route_provider_fails(self):
        original_get = navigator_mod.httpx.get

        class FakeResponse:
            def __init__(self, data):
                self._data = data

            def raise_for_status(self):
                return None

            def json(self):
                return self._data

        def fake_get(url, params=None, timeout=None, headers=None):
            if url == navigator_mod.settings.navigator_geocode_url:
                query = params.get("q")
                lat = "10.0" if query == "A" else "11.0"
                lon = "20.0" if query == "A" else "21.0"
                return FakeResponse([{"name": query, "display_name": query, "lat": lat, "lon": lon}])
            raise navigator_mod.httpx.TimeoutException("timeout")

        try:
            navigator_mod.httpx.get = fake_get
            result = navigator_mod.navigator("A", "B", response_format="structured", timeout_ms=1000)
        finally:
            navigator_mod.httpx.get = original_get

        self.assertTrue(result["result"]["degraded"])
        self.assertTrue(result["result"]["route"]["fallback_used"])
        self.assertEqual(result["result"]["route"]["provider"], "haversine")
        self.assertGreater(result["result"]["straight_line"]["distance_km"], 0)

    def test_navigator_api_panel_payload_preserves_route_fields(self):
        result = {
            "result": {
                "origin_query": "A",
                "destination_query": "B",
                "origin": {"name": "A", "display_name": "A"},
                "destination": {"name": "B", "display_name": "B"},
                "mode": "driving",
                "provider_sequence": ["nominatim", "osrm"],
                "route": {"distance_km": 12.5, "duration_text": "20 min", "fallback_used": False},
                "straight_line": {"distance_km": 10.0},
                "degraded": False,
                "narrative": {"headline": "A to B"},
            },
            "meta": {"tool": "navigator"},
        }

        panel = api_mod._panel_payload("navigator", result)

        self.assertEqual(panel["source"], "navigator")
        self.assertEqual(panel["route"]["distance_km"], 12.5)
        self.assertEqual(panel["origin"]["name"], "A")
        self.assertEqual(panel["results"][0]["title"], "A to B")

    def test_navigator_marks_region_routes_as_representative_points(self):
        original_get = navigator_mod.httpx.get

        class FakeResponse:
            def __init__(self, data):
                self._data = data

            def raise_for_status(self):
                return None

            def json(self):
                return self._data

        def fake_get(url, params=None, timeout=None, headers=None):
            if url == navigator_mod.settings.navigator_geocode_url:
                query = params.get("q")
                if query == "Haryana":
                    return FakeResponse([
                        {
                            "name": "Haryana",
                            "display_name": "Haryana, India",
                            "lat": "29.0",
                            "lon": "76.0",
                            "category": "boundary",
                            "type": "administrative",
                            "place_rank": 8,
                        }
                    ])
                return FakeResponse([
                    {
                        "name": "New Delhi",
                        "display_name": "New Delhi, Delhi, India",
                        "lat": "28.61",
                        "lon": "77.20",
                        "category": "place",
                        "type": "city",
                        "place_rank": 16,
                    }
                ])
            return FakeResponse(
                {
                    "code": "Ok",
                    "routes": [
                        {
                            "distance": 152000,
                            "duration": 7380,
                            "geometry": "",
                        }
                    ],
                }
            )

        try:
            navigator_mod.httpx.get = fake_get
            result = navigator_mod.navigator("Haryana", "New Delhi", response_format="structured")
        finally:
            navigator_mod.httpx.get = original_get

        self.assertTrue(result["result"]["degraded"])
        self.assertIn("representative coordinate", result["result"]["precision_note"])
        self.assertTrue(result["result"]["origin"]["precision"]["representative_point"])

    def test_navigator_route_places_are_returned_for_city_labels(self):
        original_samples = navigator_mod._route_sample_points
        original_reverse = navigator_mod._reverse_place
        try:
            navigator_mod._route_sample_points = lambda route: [
                {"lat": 1.0, "lon": 2.0, "fraction": 0.25},
                {"lat": 3.0, "lon": 4.0, "fraction": 0.75},
            ]
            navigator_mod._reverse_place = lambda point, timeout_seconds: (
                {
                    "name": "Route City" if point["fraction"] < 0.5 else "Second City",
                    "display_name": "Route City",
                    "lat": point["lat"],
                    "lon": point["lon"],
                    "fraction": point["fraction"],
                    "source": "nominatim_reverse",
                },
                "",
            )
            places, status, external_count = navigator_mod._route_places({"geometry": "present"}, 1.0)
        finally:
            navigator_mod._route_sample_points = original_samples
            navigator_mod._reverse_place = original_reverse

        self.assertEqual([place["name"] for place in places], ["Route City", "Second City"])
        self.assertEqual(status["returned_places"], 2)
        self.assertEqual(external_count, 2)


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

    def test_reddit_structured_search_fallback_reports_degraded_provider(self):
        original_get = reddit_mod._get
        original_ddgs = reddit_mod.DDGS
        reddit_mod._cache.clear()
        reddit_mod._cache_ts.clear()
        calls = []

        def fake_get(path, params=None, timeout_seconds=15.0, stats=None):
            reddit_mod._record_external(stats, "reddit")
            calls.append((path, timeout_seconds))
            return {"_error": "blocked"}

        class FakeDDGS:
            def __init__(self, timeout=None):
                self.timeout = timeout

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def text(self, query, max_results):
                return [{"title": "Thread", "body": "Body", "href": "https://reddit.com/r/test/1"}]

        try:
            reddit_mod._get = fake_get
            reddit_mod.DDGS = FakeDDGS
            result = reddit_mod.reddit(
                action="search",
                query="ai",
                limit="1",
                timeout_ms=2500,
                response_format="structured",
                include_source_status=True,
            )
        finally:
            reddit_mod._get = original_get
            reddit_mod.DDGS = original_ddgs
            reddit_mod._cache.clear()
            reddit_mod._cache_ts.clear()

        self.assertEqual(calls[0][1], 2.5)
        self.assertEqual(result["result"]["source"], "ddgs")
        self.assertTrue(result["result"]["fallback_used"])
        self.assertTrue(result["result"]["degraded"])
        self.assertEqual(result["result"]["count"], 1)
        self.assertEqual(result["meta"]["version"], "2.0.0")

    def test_reddit_search_reports_bounded_provider_failure(self):
        original_get = reddit_mod.httpx.get
        original_ddgs = reddit_mod.DDGS
        original_attempts = reddit_mod.settings.external_request_attempts
        original_delay = reddit_mod.settings.external_retry_delay
        reddit_mod._cache.clear()
        reddit_mod._cache_ts.clear()
        calls = []

        def failing_get(*args, **kwargs):
            calls.append(kwargs.get("timeout"))
            raise reddit_mod.httpx.TimeoutException("timeout")

        class FailingDDGS:
            def __init__(self, timeout=None):
                self.timeout = timeout

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def text(self, query, max_results):
                raise RuntimeError("blocked")

        try:
            reddit_mod.httpx.get = failing_get
            reddit_mod.DDGS = FailingDDGS
            reddit_mod.settings.external_request_attempts = 2
            reddit_mod.settings.external_retry_delay = 0
            result = reddit_mod.reddit(action="search", query="ai", limit=2, timeout_ms=2000)
        finally:
            reddit_mod.httpx.get = original_get
            reddit_mod.DDGS = original_ddgs
            reddit_mod.settings.external_request_attempts = original_attempts
            reddit_mod.settings.external_retry_delay = original_delay
            reddit_mod._cache.clear()
            reddit_mod._cache_ts.clear()

        self.assertEqual(calls, [2.0, 2.0])
        self.assertIn("Reddit is blocked from this network", result)

    def test_reddit_retries_transient_failure_then_succeeds(self):
        original_get = reddit_mod.httpx.get
        original_attempts = reddit_mod.settings.external_request_attempts
        original_delay = reddit_mod.settings.external_retry_delay
        reddit_mod._cache.clear()
        reddit_mod._cache_ts.clear()
        calls = []

        class FakeResponse:
            status_code = 200

            def raise_for_status(self):
                return None

            def json(self):
                return {
                    "kind": "Listing",
                    "data": {
                        "children": [
                            {
                                "kind": "t3",
                                "data": {
                                    "title": "Recovered",
                                    "score": 1,
                                    "author": "u",
                                    "num_comments": 0,
                                    "subreddit": "python",
                                    "created_utc": 1,
                                    "upvote_ratio": 1,
                                    "id": "abc",
                                    "permalink": "/r/python/comments/abc/recovered/",
                                },
                            }
                        ]
                    },
                }

        def flaky_get(*args, **kwargs):
            calls.append(kwargs.get("timeout"))
            if len(calls) < 3:
                raise reddit_mod.httpx.TimeoutException("timeout")
            return FakeResponse()

        try:
            reddit_mod.httpx.get = flaky_get
            reddit_mod.settings.external_request_attempts = 3
            reddit_mod.settings.external_retry_delay = 0
            result = reddit_mod.reddit(
                action="hot",
                subreddit="python",
                limit=1,
                timeout_ms=2000,
                response_format="structured",
            )
        finally:
            reddit_mod.httpx.get = original_get
            reddit_mod.settings.external_request_attempts = original_attempts
            reddit_mod.settings.external_retry_delay = original_delay
            reddit_mod._cache.clear()
            reddit_mod._cache_ts.clear()

        self.assertEqual(calls, [2.0, 2.0, 2.0])
        self.assertEqual(result["result"]["count"], 1)
        self.assertEqual(result["result"]["items"][0]["title"], "Recovered")

    def test_reddit_structured_missing_query_error_and_trace(self):
        stream = io.StringIO()

        with redirect_stdout(stream):
            result = execute_tool(
                "reddit",
                action="search",
                response_format="structured",
                trace_enabled=True,
            )

        trace = json.loads(stream.getvalue().strip())
        self.assertEqual(result["error"]["code"], "MISSING_QUERY")
        self.assertEqual(trace["tool"], "reddit")
        self.assertEqual(trace["status"], "FAILED")

    def test_hackernews_structured_search_success_uses_timeout(self):
        original_get = hn_mod.httpx.get
        calls = []

        class FakeResponse:
            def raise_for_status(self):
                return None

            def json(self):
                return {
                    "hits": [
                        {
                            "title": "HN Story",
                            "points": 5,
                            "author": "author",
                            "created_at_i": 1,
                            "num_comments": 2,
                            "objectID": "123",
                            "url": "https://example.com/story",
                        }
                    ]
                }

        def fake_get(url, params=None, timeout=None, headers=None):
            calls.append({"url": url, "timeout": timeout, "params": params})
            return FakeResponse()

        try:
            hn_mod.httpx.get = fake_get
            result = hn_mod.hackernews(
                action="search",
                query="ai",
                limit="1",
                timeout_ms=3000,
                response_format="structured",
                include_source_status=True,
            )
        finally:
            hn_mod.httpx.get = original_get

        self.assertEqual(calls[0]["timeout"], 3.0)
        self.assertEqual(result["result"]["provider"], "algolia")
        self.assertEqual(result["result"]["count"], 1)
        self.assertEqual(result["result"]["items"][0]["id"], "123")
        self.assertEqual(result["result"]["source_status"], "ok")

    def test_hackernews_retries_transient_failure_then_succeeds(self):
        original_get = hn_mod.httpx.get
        original_attempts = hn_mod.settings.external_request_attempts
        original_delay = hn_mod.settings.external_retry_delay
        calls = []

        class FakeResponse:
            def raise_for_status(self):
                return None

            def json(self):
                return {
                    "hits": [
                        {
                            "title": "Recovered HN",
                            "points": 1,
                            "author": "a",
                            "created_at_i": 1,
                            "num_comments": 0,
                            "objectID": "456",
                            "url": "https://example.com/hn",
                        }
                    ]
                }

        def flaky_get(*args, **kwargs):
            calls.append(kwargs.get("timeout"))
            if len(calls) < 3:
                raise hn_mod.httpx.TimeoutException("timeout")
            return FakeResponse()

        try:
            hn_mod.httpx.get = flaky_get
            hn_mod.settings.external_request_attempts = 3
            hn_mod.settings.external_retry_delay = 0
            result = hn_mod.hackernews(
                action="search",
                query="ai",
                limit=1,
                timeout_ms=2000,
                response_format="structured",
            )
        finally:
            hn_mod.httpx.get = original_get
            hn_mod.settings.external_request_attempts = original_attempts
            hn_mod.settings.external_retry_delay = original_delay

        self.assertEqual(calls, [2.0, 2.0, 2.0])
        self.assertEqual(result["result"]["count"], 1)
        self.assertEqual(result["result"]["items"][0]["id"], "456")

    def test_hackernews_structured_invalid_story_id_error(self):
        result = hn_mod.hackernews(
            action="comments",
            query="not-an-id",
            response_format="structured",
        )

        self.assertEqual(result["error"]["code"], "INVALID_STORY_ID")
        self.assertFalse(result["error"]["retryable"])
        self.assertIn("suggestion", result["error"])

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

    def test_web_search_structured_fallback_reports_degraded_provider(self):
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
                return [{"title": "Structured", "body": "Body", "href": "https://example.com/s"}]

        try:
            web_mod._tavily = FailingTavily()
            web_mod.DDGS = FakeDDGS
            result = web_mod.web_search(
                "query",
                max_results=1,
                response_format="structured",
            )
        finally:
            web_mod._tavily = original_tavily
            web_mod.DDGS = original_ddgs

        self.assertEqual(result["result"]["provider_used"], "ddgs")
        self.assertTrue(result["result"]["fallback_used"])
        self.assertTrue(result["result"]["degraded"])
        self.assertEqual(result["result"]["result_count"], 1)
        self.assertEqual(result["meta"]["version"], "2.0.0")

    def test_web_search_auto_supplements_thin_tavily_results_with_ddgs(self):
        original_tavily = web_mod._tavily
        original_ddgs = web_mod.DDGS

        class ThinTavily:
            def search(self, *args, **kwargs):
                return {
                    "results": [
                        {
                            "title": "Google Gemini model docs",
                            "content": "Gemini model documentation",
                            "url": "https://cloud.google.com/vertex-ai/generative-ai/docs/models",
                        }
                    ]
                }

        class FakeDDGS:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def text(self, query, max_results):
                return [
                    {"title": "Gemini API models", "body": "Model list", "href": "https://ai.google.dev/gemini-api/docs/models"},
                    {"title": "Vertex AI models", "body": "Available models", "href": "https://cloud.google.com/vertex-ai/generative-ai/docs/learn/models"},
                ][:max_results]

        try:
            web_mod._tavily = ThinTavily()
            web_mod.DDGS = FakeDDGS
            result = web_mod.web_search(
                "latest details on models from google",
                max_results=3,
                response_format="structured",
            )
        finally:
            web_mod._tavily = original_tavily
            web_mod.DDGS = original_ddgs

        self.assertEqual(result["result"]["result_count"], 3)
        self.assertTrue(result["result"]["supplemental_used"])
        self.assertEqual(result["result"]["provider_sequence"], ["tavily", "ddgs"])

    def test_web_search_structured_empty_query_error(self):
        result = web_mod.web_search("", response_format="structured")

        self.assertEqual(result["error"]["code"], "EMPTY_QUERY")
        self.assertFalse(result["error"]["retryable"])
        self.assertIn("suggestion", result["error"])

    def test_web_fetch_structured_success_and_trace(self):
        original_get = web_mod.httpx.get

        class FakeResponse:
            status_code = 200
            url = "https://example.com/final"
            text = "<html><title>Example</title><body>Hello page</body></html>"

            def raise_for_status(self):
                return None

        calls = []

        def fake_get(*args, **kwargs):
            calls.append(kwargs)
            return FakeResponse()

        stream = io.StringIO()
        try:
            web_mod.httpx.get = fake_get
            with redirect_stdout(stream):
                result = execute_tool(
                    "web_fetch",
                    url="https://example.com/start",
                    max_chars=1000,
                    timeout_ms=5000,
                    follow_redirects=False,
                    response_format="structured",
                    trace_enabled=True,
                )
        finally:
            web_mod.httpx.get = original_get

        trace = json.loads(stream.getvalue().strip())
        self.assertEqual(result["result"]["status_code"], 200)
        self.assertEqual(result["result"]["title"], "Example")
        self.assertIn("Hello page", result["result"]["text"])
        self.assertFalse(result["result"]["follow_redirects"])
        self.assertEqual(calls[0]["timeout"], 5.0)
        self.assertFalse(calls[0]["follow_redirects"])
        self.assertEqual(trace["tool"], "web_fetch")
        self.assertEqual(trace["status"], "SUCCESS")

    def test_web_fetch_structured_invalid_url_error(self):
        result = web_mod.web_fetch("example.com/no-scheme", response_format="structured")

        self.assertEqual(result["error"]["code"], "INVALID_URL")
        self.assertEqual(result["error"]["field"], "url")
        self.assertFalse(result["error"]["retryable"])


if __name__ == "__main__":
    unittest.main()
