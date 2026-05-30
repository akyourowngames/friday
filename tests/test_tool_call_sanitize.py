"""Regression tests for tool-call argument sanitization.

Covers the bug where streamed tool-call argument deltas concatenated into invalid
JSON, got stored in conversation history, and 400'd the chat API on every
subsequent turn (permanently breaking the session). Also covers the direct-path
fix that no longer dumps a raw structured-response envelope to the user.
"""

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.llm import _sanitize_arguments_string, _sanitize_message_tool_calls
from agent.core import (
    _sanitize_tool_arguments,
    _structured_result_payload,
    _extract_token_tool_shape,
    _looks_like_tool_token_prefix,
    _try_parse_json_tool_call,
)

# The exact corruption observed in the field.
_CORRUPT = '{"response_format": "structured, "trace_enabled": false{"response_format": "structured", "trace_enabled": false}'


class SanitizeArgumentsTests(unittest.TestCase):
    def test_repairs_concatenated_corruption(self):
        for fixer in (_sanitize_arguments_string, _sanitize_tool_arguments):
            out = fixer(_CORRUPT)
            parsed = json.loads(out)  # must not raise
            self.assertEqual(parsed.get("response_format"), "structured")
            self.assertEqual(parsed.get("trace_enabled"), False)

    def test_valid_passthrough(self):
        for fixer in (_sanitize_arguments_string, _sanitize_tool_arguments):
            self.assertEqual(json.loads(fixer('{"a": 1}')), {"a": 1})

    def test_empty_and_garbage_fall_back_to_empty_object(self):
        for fixer in (_sanitize_arguments_string, _sanitize_tool_arguments):
            self.assertEqual(fixer(""), "{}")
            self.assertEqual(fixer("not json at all"), "{}")

    def test_unrecoverable_partial_falls_back(self):
        partial = '{"response_format": "structured, "trace_enabled": false'
        for fixer in (_sanitize_arguments_string, _sanitize_tool_arguments):
            self.assertEqual(fixer(partial), "{}")


class SanitizeHistoryTests(unittest.TestCase):
    def test_poisoned_history_is_repaired(self):
        messages = [
            {"role": "user", "content": "show me my projects"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"id": "c1", "type": "function", "function": {"name": "project_status", "arguments": _CORRUPT}},
                ],
            },
        ]
        cleaned = _sanitize_message_tool_calls(messages)
        args = cleaned[1]["tool_calls"][0]["function"]["arguments"]
        json.loads(args)  # must not raise
        # Original input must not be mutated in place.
        self.assertEqual(messages[1]["tool_calls"][0]["function"]["arguments"], _CORRUPT)

    def test_clean_history_untouched(self):
        messages = [
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "t", "arguments": '{"a": 1}'}},
            ]},
        ]
        cleaned = _sanitize_message_tool_calls(messages)
        self.assertIs(cleaned, messages)

    def test_messages_without_tool_calls_untouched(self):
        messages = [{"role": "user", "content": "hi"}]
        self.assertIs(_sanitize_message_tool_calls(messages), messages)


class StructuredPayloadDetectionTests(unittest.TestCase):
    def test_detects_structured_envelope(self):
        envelope = json.dumps({"result": {"x": 1}, "meta": {"tool": "t"}})
        self.assertIsNotNone(_structured_result_payload(envelope))

    def test_detects_error_envelope(self):
        envelope = json.dumps({"error": {"code": "X"}, "meta": {"tool": "t"}})
        self.assertIsNotNone(_structured_result_payload(envelope))

    def test_plain_string_is_not_structured(self):
        self.assertIsNone(_structured_result_payload("Saved note 'foo'"))
        self.assertIsNone(_structured_result_payload(""))
        self.assertIsNone(_structured_result_payload('{"just": "a dict"}'))


class TokenToolCallLeakTests(unittest.TestCase):
    SCHEMAS = [{"function": {"name": "project_status"}}, {"function": {"name": "reminder"}}]

    def test_extracts_bare_token(self):
        name, args, is_tok = _extract_token_tool_shape("[TOOL_CALLS]project_status{}")
        self.assertTrue(is_tok)
        self.assertEqual(name, "project_status")
        self.assertEqual(args, {})

    def test_extracts_token_without_braces(self):
        name, args, is_tok = _extract_token_tool_shape("[TOOL_CALLS]project_status")
        self.assertTrue(is_tok)
        self.assertEqual(name, "project_status")
        self.assertEqual(args, {})

    def test_extracts_token_with_args(self):
        name, args, is_tok = _extract_token_tool_shape('[TOOL_CALLS]reminder{"task": "call mom", "when": "5pm"}')
        self.assertEqual(name, "reminder")
        self.assertEqual(args, {"task": "call mom", "when": "5pm"})

    def test_token_with_leading_text(self):
        name, _, is_tok = _extract_token_tool_shape("Sure! [TOOL_CALLS]project_status{}")
        self.assertTrue(is_tok)
        self.assertEqual(name, "project_status")

    def test_plain_text_is_not_a_token(self):
        name, args, is_tok = _extract_token_tool_shape("just a normal sentence")
        self.assertFalse(is_tok)

    def test_recovery_routes_token_to_tool_call(self):
        tc, err = _try_parse_json_tool_call("[TOOL_CALLS]project_status{}", self.SCHEMAS)
        self.assertIsNone(err)
        self.assertIsNotNone(tc)
        self.assertEqual(tc["name"], "project_status")
        json.loads(tc["arguments"])  # valid

    def test_recovery_reports_unknown_tool(self):
        tc, err = _try_parse_json_tool_call("[TOOL_CALLS]nope{}", self.SCHEMAS)
        self.assertIsNone(tc)
        self.assertIn("not an available tool", err)

    def test_prefix_detection_holds_back_partial_token(self):
        self.assertTrue(_looks_like_tool_token_prefix("[TOOL"))
        self.assertTrue(_looks_like_tool_token_prefix("[TOOL_CALLS]proj"))
        self.assertFalse(_looks_like_tool_token_prefix("Good morning, sir"))
        self.assertFalse(_looks_like_tool_token_prefix(""))


if __name__ == "__main__":
    unittest.main()
