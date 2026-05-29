import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tools
from agent.core import (
    _build_system_prompt,
    _expand_conversational_input,
    _is_conversational_turn,
    _merge_context_facts,
    _should_extract_memory,
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


if __name__ == "__main__":
    unittest.main()
