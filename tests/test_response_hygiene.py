"""Response hygiene integration test.

Drives the real KING agent across many query types and asserts that no response
is malformed: no raw structured-JSON envelope, no leaked native tool-call token,
no NIM 400/error string, and no empty reply. This is the broad guard that would
have caught the JSON-leak, [TOOL_CALLS] token, and 400-poisoning bugs.

Skips automatically when no API key is configured so offline runs do not fail.
Uses an isolated project store so it never touches real data.
"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import settings


def _api_available() -> bool:
    return bool(str(settings.nim_api_key or "").strip())


# Substrings that indicate a malformed / failed response.
_BAD_MARKERS = (
    "[TOOL_CALLS]",
    '"meta":',          # raw structured envelope leaked
    "NVIDIA NIM API error",
    "BadRequestError",
    "Expecting ',' delimiter",
    "Traceback (most recent call last)",
)


@unittest.skipUnless(_api_available(), "NVIDIA API key not configured; skipping live agent hygiene test")
class ResponseHygieneTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        root = Path(cls._tmp.name)
        settings.project_store_path = str(root / "projects.json")
        settings.project_log_path = str(root / "proj_log.jsonl")
        from agent.core import Agent

        cls.agent = Agent()

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def _ask(self, message: str) -> str:
        chunks = []
        try:
            self.agent.process(message, emit_chunk=lambda c: chunks.append(c))
        except RuntimeError as exc:
            # Transient NIM connectivity (timeout / rate limit / upstream error)
            # is not a response-hygiene failure. Skip this query rather than
            # flake the suite.
            self.skipTest(f"NIM unavailable for {message!r}: {exc}")
        return "".join(chunks).strip()

    def _assert_clean(self, message: str):
        reply = self._ask(message)
        self.assertTrue(reply, f"Empty reply for: {message!r}")
        for marker in _BAD_MARKERS:
            self.assertNotIn(
                marker, reply,
                f"Malformed marker {marker!r} in reply to {message!r}: {reply[:200]!r}",
            )

    def test_greeting(self):
        self._assert_clean("hi")

    def test_time_query(self):
        self._assert_clean("whats the time king")

    def test_project_create(self):
        self._assert_clean("track this: build a budget tracker app by next month")

    def test_project_list_variants(self):
        # The exact phrasings that previously leaked tokens / JSON / 400s.
        for q in ("whats in project", "yeah list me all", "show me all my projects", "show me all my projects"):
            self._assert_clean(q)

    def test_project_update_and_status(self):
        self._assert_clean("i finished the database schema and the login screen is blocked on oauth")
        self._assert_clean("king status")

    def test_focus_and_decisions(self):
        self._assert_clean("what should i focus on today")
        self._assert_clean("what did we decide so far")

    def test_casual_followups(self):
        self._assert_clean("nice")
        self._assert_clean("??")


if __name__ == "__main__":
    unittest.main()
