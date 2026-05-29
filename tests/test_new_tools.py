"""Tests for the 10 new KING tools and the relative-time parser.

Offline and deterministic where possible. Network/desktop tools are tested for
graceful degradation and structured shape, not live external behavior.
"""

import sys
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tools  # noqa: F401  (registers all tools)
from tools.registry import get_tool, execute_tool
from tools.timeparse import parse_relative_seconds, resolve_when


class TimeParseTests(unittest.TestCase):
    def test_simple_minutes(self):
        self.assertEqual(parse_relative_seconds("in 5 min"), 300)

    def test_attached_unit(self):
        self.assertEqual(parse_relative_seconds("10m"), 600)

    def test_compound(self):
        self.assertEqual(parse_relative_seconds("1 hour 30 minutes"), 5400)

    def test_no_unit_returns_none(self):
        self.assertIsNone(parse_relative_seconds("later today"))

    def test_resolve_iso(self):
        when, mode = resolve_when("2026-05-30T09:00:00", datetime(2026, 5, 29))
        self.assertEqual(mode, "iso")
        self.assertEqual(when.hour, 9)

    def test_resolve_relative(self):
        base = datetime(2026, 5, 29, 10, 0, 0)
        when, mode = resolve_when("in 2 hours", base)
        self.assertEqual(mode, "relative")
        self.assertEqual(when.hour, 12)


class RegistrationTests(unittest.TestCase):
    def test_all_new_tools_registered(self):
        for name in (
            "reminder", "reminder_fire", "clipboard", "screenshot",
            "system_pulse", "weather", "calc", "process_control",
            "life_timeline", "proactive_check",
        ):
            self.assertIsNotNone(get_tool(name), f"{name} not registered")


class CalcTests(unittest.TestCase):
    def test_basic_arithmetic(self):
        result = execute_tool("calc", expression="2 + 3 * 4", response_format="structured")
        self.assertEqual(result["result"]["value"], 14)

    def test_percentage_pattern(self):
        result = execute_tool("calc", expression="0.15 * 2400", response_format="structured")
        self.assertEqual(result["result"]["value"], 360.0)

    def test_function_and_constant(self):
        result = execute_tool("calc", expression="sqrt(144) + 3**2", response_format="structured")
        self.assertEqual(result["result"]["value"], 21.0)

    def test_rejects_unsafe_names(self):
        result = execute_tool("calc", expression="__import__('os')", response_format="structured")
        self.assertIn(result["error"]["code"], ("UNSAFE_EXPRESSION", "INVALID_EXPRESSION"))

    def test_division_by_zero(self):
        result = execute_tool("calc", expression="1/0", response_format="structured")
        self.assertEqual(result["error"]["code"], "DIVISION_BY_ZERO")


class ClipboardTests(unittest.TestCase):
    def test_write_then_read_roundtrip(self):
        import tools.clipboard as clip_mod

        if clip_mod._backend() is None:
            self.skipTest("no clipboard backend")
        payload = "KING clipboard test 12345"
        write = execute_tool("clipboard", action="write", text=payload, response_format="structured")
        self.assertIn("result", write)
        read = execute_tool("clipboard", action="read", response_format="structured")
        self.assertEqual(read["result"]["content"], payload)

    def test_write_empty_rejected(self):
        result = execute_tool("clipboard", action="write", text="", response_format="structured")
        self.assertEqual(result["error"]["code"], "EMPTY_TEXT")


class CalcAndPulseTests(unittest.TestCase):
    def test_system_pulse_structured(self):
        result = execute_tool("system_pulse", top_n=3, response_format="structured")
        # Either psutil is present (success) or it degrades to a typed error.
        if "result" in result:
            self.assertIn("cpu_percent", result["result"])
            self.assertIn("memory", result["result"])
        else:
            self.assertEqual(result["error"]["code"], "PSUTIL_UNAVAILABLE")


class ProcessControlTests(unittest.TestCase):
    def test_find_nonexistent_process_is_partial(self):
        result = execute_tool("process_control", name="definitely_not_a_real_process_xyz", action="find", response_format="structured")
        if "result" in result:
            self.assertEqual(result["result"]["count"], 0)
        else:
            self.assertEqual(result["error"]["code"], "PSUTIL_UNAVAILABLE")

    def test_invalid_action_rejected(self):
        result = execute_tool("process_control", name="python", action="explode", response_format="structured")
        # Reaches action validation only if psutil is present; otherwise backend error.
        self.assertIn(result.get("error", {}).get("code"), ("INVALID_ACTION", "PSUTIL_UNAVAILABLE"))

    def test_empty_name_rejected(self):
        result = execute_tool("process_control", name="", action="find", response_format="structured")
        self.assertEqual(result["error"]["code"], "EMPTY_NAME")


class ReminderTests(unittest.TestCase):
    def test_reminder_rejects_unresolved_time(self):
        result = execute_tool("reminder", task="do hw", when="sometime", response_format="structured")
        self.assertEqual(result["error"]["code"], "UNRESOLVED_TIME")

    def test_reminder_rejects_empty_task(self):
        result = execute_tool("reminder", task="", when="in 5 min", response_format="structured")
        self.assertEqual(result["error"]["code"], "EMPTY_TASK")


class LifeTimelineTests(unittest.TestCase):
    def test_life_timeline_structured_shape(self):
        result = execute_tool("life_timeline", limit=5, response_format="structured")
        self.assertIn("result", result)
        self.assertIn("episodes", result["result"])
        self.assertIsInstance(result["result"]["episodes"], list)


class ProactiveCheckTests(unittest.TestCase):
    def test_proactive_check_returns_decision(self):
        result = execute_tool("proactive_check", situational_fit=0.7, response_format="structured")
        self.assertIn("result", result)
        self.assertIn("has_message", result["result"])


if __name__ == "__main__":
    unittest.main()
