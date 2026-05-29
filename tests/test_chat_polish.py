import contextlib
import io
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tools
from agent.core import (
    Agent,
    _build_system_prompt,
    _expand_conversational_input,
    _is_conversational_turn,
    _looks_like_actionable_request,
    _merge_context_facts,
    _selected_tools_are_memory_recall_only,
    _should_extract_memory,
    _should_use_proactive_memory_context,
    _strip_memory_prefix,
)


class ChatPolishTests(unittest.TestCase):
    def test_memory_facts_live_in_system_prompt_not_user_prefix(self):
        prompt = _build_system_prompt(
            [],
            memory_facts="User name is Krish",
            conversational_turn=True,
        )
        self.assertIn("Known facts for this turn:", prompt)
        self.assertIn("User name is Krish", prompt)
        self.assertIn("Memory answer priority", prompt)
        self.assertNotIn("PERMANENT MEMORY FACTS", prompt)

    def test_merge_keeps_specific_recall_ahead_of_profile(self):
        specific = "User has JEE backlog | User is preparing for JEE"
        profile = "User name is Krish Verma | ankita lives in haryana"
        merged = _merge_context_facts(specific, profile, limit=8)
        self.assertTrue(merged.startswith("User has JEE backlog"))
        self.assertIn("User name is Krish Verma", merged)

    def test_merge_dedupes_facts_ignoring_via_suffix(self):
        primary = "Ankita is in class 11th (via ankita in class 11th)"
        secondary = "Ankita is in class 11th | User name is Krish Verma"
        merged = _merge_context_facts(primary, secondary, limit=8)
        self.assertEqual(merged.count("Ankita is in class 11th"), 1)
        self.assertIn("User name is Krish Verma", merged)


    def test_expand_incomplete_follow_up(self):
        messages = [
            {"role": "user", "content": "ahh who i am"},
            {"role": "assistant", "content": "You are Krish Verma, sir."},
        ]
        expanded = _expand_conversational_input("and who is my....", messages)
        self.assertIn("Earlier:", expanded)
        self.assertIn("and who is my", expanded)

    def test_banter_skips_memory_extraction(self):
        from agent.embedder import embed

        self.assertFalse(_should_extract_memory("you are being smarter huh", embed("you are being smarter huh")))

    def test_conversational_turn_without_tools(self):
        self.assertTrue(_is_conversational_turn([], "how are you man", None))

    def test_strip_memory_prefix(self):
        raw = "[PERMANENT MEMORY FACTS: x]\nhello"
        self.assertEqual(_strip_memory_prefix(raw), "hello")

    def test_proactive_memory_context_uses_semantic_gate(self):
        class FakeRouteEmbeddings:
            def v(self, name):
                values = {
                    "proactive_memory": np.array([1.0, 0.0], dtype=np.float32),
                    "no_memory_small_talk": np.array([0.98, 0.0], dtype=np.float32),
                    "actionable": np.array([0.2, 0.0], dtype=np.float32),
                }
                return values[name]

        q_emb = np.array([1.0, 0.0], dtype=np.float32)
        with patch("agent.core._RouteEmbeddings.get", return_value=FakeRouteEmbeddings()):
            self.assertTrue(_should_use_proactive_memory_context("how is my prep going", q_emb, []))
            self.assertFalse(
                _should_use_proactive_memory_context(
                    "how is my prep going",
                    q_emb,
                    [{"name": "web_search"}],
                )
            )

    def test_memory_question_is_not_actionable_discovery(self):
        class FakeRouteEmbeddings:
            def v(self, name):
                values = {
                    "actionable": np.array([0.2, 0.0], dtype=np.float32),
                    "banter": np.array([0.1, 0.0], dtype=np.float32),
                    "no_memory_small_talk": np.array([0.1, 0.0], dtype=np.float32),
                    "memory_recall": np.array([1.0, 0.0], dtype=np.float32),
                }
                return values[name]

        q_emb = np.array([1.0, 0.0], dtype=np.float32)
        with patch("agent.core._RouteEmbeddings.get", return_value=FakeRouteEmbeddings()):
            self.assertFalse(_looks_like_actionable_request("who is rai", q_emb))

    def test_process_does_not_print_thinking_status(self):
        class FakeDelta:
            content = "Rai is your friend, sir."
            tool_calls = None

        class FakeChoice:
            delta = FakeDelta()

        class FakeChunk:
            choices = [FakeChoice()]

        class FakeLlm:
            def stream(self, messages, tools=None):
                return iter([FakeChunk()])

        class FakeRouter:
            def select_tools(self, query, q_emb=None):
                return []

            def last_decision(self):
                return {"reason": "below_tool_threshold"}

            def capability_hint(self, tool_name):
                return {}

        class FakeBrain:
            def recall_context(self, query, limit, q_emb=None):
                return ""

        class FakeVerifier:
            def verify(self, content, current_turn_called, current_turn_tool_msgs, tool_schemas):
                return "PASS"

        agent = object.__new__(Agent)
        agent.messages = [{"role": "system", "content": "test"}]
        agent.llm = FakeLlm()
        agent.router = FakeRouter()
        agent.validator = None
        agent.verifier = FakeVerifier()
        agent.brain = FakeBrain()
        agent._memory_extraction_messages = []
        agent._last_memory_profile_context = False
        agent._summary_context = ""
        agent._maybe_summarize = lambda: None
        agent._submit_background = lambda *args, **kwargs: None

        output = io.StringIO()
        with patch("agent.core._embedding_query", return_value=(None, "who is rai")):
            with contextlib.redirect_stdout(output):
                content = agent.process("who is rai")

        self.assertEqual(content, "Rai is your friend, sir.")
        self.assertIn("Rai is your friend, sir.", output.getvalue())
        self.assertNotIn("Thinking", output.getvalue())

    def test_process_emits_streamed_chunks_to_callback(self):
        class FakeChoice:
            def __init__(self, text):
                self.delta = type("FakeDelta", (), {"content": text, "tool_calls": None})()

        class FakeChunk:
            def __init__(self, text):
                self.choices = [FakeChoice(text)]

        class FakeLlm:
            def stream(self, messages, tools=None):
                return iter([FakeChunk("hello "), FakeChunk("sir")])

        class FakeRouter:
            def select_tools(self, query, q_emb=None):
                return []

            def last_decision(self):
                return {"reason": "below_tool_threshold"}

            def capability_hint(self, tool_name):
                return {}

        class FakeBrain:
            def recall_context(self, query, limit, q_emb=None):
                return ""

        class FakeVerifier:
            def verify(self, content, current_turn_called, current_turn_tool_msgs, tool_schemas):
                return "PASS"

        agent = object.__new__(Agent)
        agent.messages = [{"role": "system", "content": "test"}]
        agent.llm = FakeLlm()
        agent.router = FakeRouter()
        agent.validator = None
        agent.verifier = FakeVerifier()
        agent.brain = FakeBrain()
        agent._memory_extraction_messages = []
        agent._last_memory_profile_context = False
        agent._summary_context = ""
        agent._maybe_summarize = lambda: None
        agent._submit_background = lambda *args, **kwargs: None

        chunks = []
        with patch("agent.core._embedding_query", return_value=(None, "hello")):
            with contextlib.redirect_stdout(io.StringIO()):
                content = agent.process("hello", emit_chunk=chunks.append)

        self.assertEqual(content, "hello sir")
        self.assertEqual(chunks, ["hello ", "sir"])

    def test_memory_recall_only_is_safe_to_answer_from_prompt_context(self):
        self.assertTrue(_selected_tools_are_memory_recall_only([{"name": "memory_recall"}]))
        self.assertFalse(_selected_tools_are_memory_recall_only([{"name": "memory_remember"}]))
        self.assertFalse(_selected_tools_are_memory_recall_only([{"name": "memory_recall"}, {"name": "memory_forget"}]))


if __name__ == "__main__":
    unittest.main()
