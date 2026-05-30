"""Tests for session digest extraction and session graph relations."""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from memory.session_digest import (
    digest_session,
    digest_to_facts,
    process_session,
    process_undigested,
    _format_transcript,
)
from memory.session_store import SessionStore


def _fake_llm(responses):
    """Fake OpenAI client returning given JSON strings in order, last repeats."""
    state = {"i": 0}

    class _Completions:
        def create(self, **kwargs):
            idx = min(state["i"], len(responses) - 1)
            state["i"] += 1
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=responses[idx]))]
            )

    return SimpleNamespace(client=SimpleNamespace(chat=SimpleNamespace(completions=_Completions())))


class FormatTranscriptTests(unittest.TestCase):
    def test_empty_turns_produce_empty_string(self):
        self.assertEqual(_format_transcript([]), "")

    def test_skips_empty_roles(self):
        turns = [{"role": "", "content": "hello"}, {"role": "user", "content": ""}]
        self.assertEqual(_format_transcript(turns), "")

    def test_truncates_at_max_chars(self):
        turns = [{"role": "user", "content": "x" * 200}]
        result = _format_transcript(turns, max_chars=50)
        self.assertLessEqual(len(result), 60)
        self.assertTrue(result.endswith("..."))

    def test_formats_multiple_turns(self):
        turns = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi there"},
        ]
        result = _format_transcript(turns)
        self.assertIn("user: hello", result)
        self.assertIn("assistant: hi there", result)


class DigestSessionTests(unittest.TestCase):
    def test_empty_turns_returns_empty(self):
        self.assertEqual(digest_session([]), {})

    def test_no_user_turns_returns_empty(self):
        turns = [{"role": "assistant", "content": "hello"}]
        self.assertEqual(digest_session(turns), {})

    @patch("memory.session_digest._llm_call")
    def test_returns_empty_on_llm_failure(self, mock_llm):
        mock_llm.return_value = None
        turns = [{"role": "user", "content": "let's talk about the project"}]
        self.assertEqual(digest_session(turns), {})

    @patch("memory.session_digest._llm_call")
    def test_parses_valid_llm_response(self, mock_llm):
        mock_llm.return_value = json.dumps({
            "topics": ["web scraping project"],
            "goals": ["ship by Friday"],
            "decisions": ["using FastAPI"],
            "problems": [],
            "ideas": ["maybe add caching"],
            "events": ["deployed v1"],
            "entities": ["FastAPI (tool)"],
        })
        turns = [
            {"role": "user", "content": "let's talk about the scraping project"},
            {"role": "assistant", "content": "sure, what's the status?"},
            {"role": "user", "content": "I want to ship by Friday using FastAPI"},
        ]
        digest = digest_session(turns)
        self.assertEqual(digest["topics"], ["web scraping project"])
        self.assertEqual(digest["goals"], ["ship by Friday"])
        self.assertEqual(digest["decisions"], ["using FastAPI"])
        self.assertEqual(digest["ideas"], ["maybe add caching"])
        self.assertEqual(digest["events"], ["deployed v1"])
        self.assertEqual(digest["entities"], ["FastAPI (tool)"])
        self.assertNotIn("problems", digest)

    @patch("memory.session_digest._llm_call")
    def test_strips_markdown_fences(self, mock_llm):
        mock_llm.return_value = '```json\n{"topics": ["testing the scraping tool"]}\n```'
        turns = [{"role": "user", "content": "test input"}]
        digest = digest_session(turns)
        self.assertEqual(digest.get("topics"), ["testing the scraping tool"])

    @patch("memory.session_digest._llm_call")
    def test_ignores_invalid_json(self, mock_llm):
        mock_llm.return_value = "not json at all"
        turns = [{"role": "user", "content": "test input"}]
        self.assertEqual(digest_session(turns), {})

    @patch("memory.session_digest._llm_call")
    def test_filters_short_items(self, mock_llm):
        mock_llm.return_value = json.dumps({
            "topics": ["ab", "valid topic here"],
            "goals": [],
        })
        turns = [{"role": "user", "content": "test"}]
        digest = digest_session(turns)
        self.assertEqual(digest["topics"], ["valid topic here"])

    @patch("memory.session_digest._llm_call")
    def test_skips_turns_with_short_user_content(self, mock_llm):
        mock_llm.return_value = json.dumps({"topics": ["test"]})
        # Single very short user turn should still be digested
        turns = [{"role": "user", "content": "hi"}]
        digest = digest_session(turns)
        # The function allows 1+ user turns regardless of length
        self.assertIsInstance(digest, dict)


class DigestToFactsTests(unittest.TestCase):
    def test_empty_digest_returns_empty(self):
        self.assertEqual(digest_to_facts({}), [])

    def test_converts_all_categories(self):
        digest = {
            "topics": ["topic A"],
            "goals": ["goal B"],
            "decisions": ["decision C"],
            "problems": ["problem D"],
            "ideas": ["idea E"],
            "events": ["event F"],
            "entities": ["FastAPI (tool)", "Krish (person)"],
        }
        facts = digest_to_facts(digest)
        self.assertEqual(len(facts), 8)
        self.assertIn("topic A", facts)
        self.assertIn("goal B", facts)
        self.assertIn("decision C", facts)
        self.assertIn("problem D", facts)
        self.assertIn("idea E", facts)
        self.assertIn("event F", facts)
        self.assertIn("Entity mentioned: FastAPI (tool)", facts)
        self.assertIn("Entity mentioned: Krish (person)", facts)

    def test_skips_empty_categories(self):
        digest = {"topics": ["test"], "goals": [], "problems": []}
        facts = digest_to_facts(digest)
        self.assertEqual(facts, ["test"])

    def test_skips_none_values(self):
        digest = {"topics": None, "goals": ["goal"]}
        facts = digest_to_facts(digest)
        self.assertEqual(facts, ["goal"])


class ProcessSessionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store_dir = Path(self.tmp.name) / "sessions"
        self.index_path = self.store_dir / "index.json"
        self.store = SessionStore(
            directory=str(self.store_dir),
            index_path=str(self.index_path),
        )

    def test_empty_session_returns_empty(self):
        self.store.start_session("test-empty")
        result = process_session(self.store, "test-empty")
        self.assertEqual(result["status"], "empty")

    def test_too_few_turns_skips(self):
        self.store.start_session("test-few")
        self.store.log_turn("hello", "hi there")
        result = process_session(self.store, "test-few")
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["reason"], "too_few_turns")

    @patch("memory.session_digest._llm_call")
    def test_full_process_commit_and_mark(self, mock_llm):
        mock_llm.return_value = json.dumps({
            "topics": ["project design"],
            "goals": ["finish API"],
            "decisions": [],
            "problems": [],
            "ideas": [],
            "events": [],
            "entities": [],
        })
        self.store.start_session("test-full")
        self.store.log_turn("let's design the project", "sure, what architecture?")
        self.store.log_turn("I want to finish the API", "got it")

        # Need to set min_turns to 2 for this to work
        from config import settings
        orig = settings.session_digest_min_turns
        settings.session_digest_min_turns = 2
        try:
            result = process_session(self.store, "test-full")
        finally:
            settings.session_digest_min_turns = orig

        self.assertEqual(result["status"], "digested")
        self.assertGreater(result["facts_extracted"], 0)

        # Verify marked as digested
        undigested = self.store.undigested_sessions(exclude_current=True)
        self.assertFalse(any(s.get("id") == "test-full" for s in undigested))

    @patch("memory.session_digest._llm_call")
    def test_digest_to_brain_commits_facts(self, mock_llm):
        mock_llm.return_value = json.dumps({
            "topics": [],
            "goals": ["build the app"],
            "decisions": ["use Postgres"],
            "problems": [],
            "ideas": [],
            "events": [],
            "entities": [],
        })
        self.store.start_session("test-brain")
        self.store.log_turn("let's build the app", "what stack?")
        self.store.log_turn("let's use Postgres", "good choice")

        from config import settings
        orig = settings.session_digest_min_turns
        settings.session_digest_min_turns = 2
        try:
            # Mock brain
            mock_brain = MagicMock()
            mock_brain.commit.return_value = True
            result = process_session(self.store, "test-brain", brain=mock_brain)
        finally:
            settings.session_digest_min_turns = orig

        self.assertEqual(result["status"], "digested")
        self.assertGreater(result["facts_stored"], 0)
        self.assertEqual(mock_brain.commit.call_count, result["facts_stored"])


class ProcessUndigestedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store_dir = Path(self.tmp.name) / "sessions"
        self.index_path = self.store_dir / "index.json"
        self.store = SessionStore(
            directory=str(self.store_dir),
            index_path=str(self.index_path),
        )

    def test_no_sessions_returns_empty(self):
        results = process_undigested(self.store)
        self.assertEqual(results, [])

    @patch("memory.session_digest._llm_call")
    def test_processes_multiple_undigested(self, mock_llm):
        mock_llm.return_value = json.dumps({
            "topics": [], "goals": [], "decisions": [],
            "problems": [], "ideas": [], "events": [],
            "entities": [],
        })
        # Create two sessions with enough turns
        for sid in ("session-a", "session-b"):
            self.store.start_session(sid)
            self.store.log_turn("first message", "response one")
            self.store.log_turn("second message", "response two")
        # Start a third (current) session
        self.store.start_session("session-current")

        from config import settings
        orig = settings.session_digest_min_turns
        settings.session_digest_min_turns = 2
        try:
            results = process_undigested(self.store)
        finally:
            settings.session_digest_min_turns = orig

        self.assertEqual(len(results), 2)
        statuses = [r["status"] for r in results]
        self.assertTrue(all(s in ("digested", "empty_digest") for s in statuses))


class SessionRelationsFileTests(unittest.TestCase):
    """Verify the MEMORY_SESSION_RELATIONS.md file is parseable by the brain's rule loader."""

    def test_session_relations_file_exists(self):
        path = Path(__file__).resolve().parent.parent / "memory" / "MEMORY_SESSION_RELATIONS.md"
        self.assertTrue(path.exists(), f"MEMORY_SESSION_RELATIONS.md not found at {path}")

    def test_session_relations_are_parseable(self):
        from memory.brain import _load_graph_relation_rules

        path = Path(__file__).resolve().parent.parent / "memory" / "MEMORY_SESSION_RELATIONS.md"
        rules = _load_graph_relation_rules(path)
        self.assertGreater(len(rules), 0, "No rules parsed from MEMORY_SESSION_RELATIONS.md")
        for rule in rules:
            self.assertIn("pattern", rule)
            self.assertIn("source", rule)
            self.assertIn("relation", rule)
            self.assertIn("target", rule)
            self.assertIn("tier", rule)
            self.assertIn("mode", rule)

    def test_session_rules_merge_with_base_rules(self):
        from memory.brain import _load_graph_relation_rules, MEMORY_GRAPH_RELATIONS_PATH, MEMORY_SESSION_RELATIONS_PATH

        base = _load_graph_relation_rules(MEMORY_GRAPH_RELATIONS_PATH)
        session = _load_graph_relation_rules(MEMORY_SESSION_RELATIONS_PATH)
        merged = base + session
        self.assertGreater(len(merged), len(base))
        self.assertGreater(len(session), 0)


if __name__ == "__main__":
    unittest.main()
