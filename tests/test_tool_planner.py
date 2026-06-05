from __future__ import annotations

import json
import unittest

from assistant_cli.tool_planner import parse_tool_plan, tool_result_context
from assistant_cli.tools.core import ToolResult


class ToolPlannerTests(unittest.TestCase):
    def test_parse_valid_plan(self) -> None:
        plan = parse_tool_plan(
            json.dumps({"tool": "weather", "arguments": {"location": "Delhi"}, "confidence": 0.91}),
            {"weather"},
        )

        self.assertTrue(plan.uses_tool)
        self.assertEqual(plan.tool, "weather")
        self.assertEqual(plan.arguments["location"], "Delhi")
        self.assertGreater(plan.confidence, 0.9)

    def test_parse_unknown_or_none_plan(self) -> None:
        unknown = parse_tool_plan(
            json.dumps({"tool": "install_pyweather", "arguments": {}, "confidence": 0.9}),
            {"weather"},
        )
        none = parse_tool_plan(json.dumps({"tool": "none", "arguments": {}, "confidence": 0.0}), {"weather"})

        self.assertFalse(unknown.uses_tool)
        self.assertFalse(none.uses_tool)

    def test_tool_result_context_feeds_final_llm(self) -> None:
        plan = parse_tool_plan(
            json.dumps({"tool": "weather", "arguments": {"location": "Delhi"}, "confidence": 0.9}),
            {"weather"},
        )
        result = ToolResult(
            tool="weather",
            ok=True,
            text="Weather for Delhi\nTemperature: 29.8 degC",
            data={"place": "Delhi"},
            latency_ms=120,
        )
        context = tool_result_context(plan, result, max_chars=2000)

        self.assertIn("registered local tool", context)
        self.assertIn("Weather for Delhi", context)
        self.assertIn("answer naturally", context)


if __name__ == "__main__":
    unittest.main()
