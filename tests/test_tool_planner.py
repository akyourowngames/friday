from __future__ import annotations

import json
import unittest
from pathlib import Path

from assistant_cli.tool_planner import candidate_tool_specs, parse_tool_plan, tool_result_context
from assistant_cli.tools import build_default_registry
from assistant_cli.tools.core import ToolResult
from test_tools import make_settings


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

    def test_registry_prefilter_skips_casual_chat(self) -> None:
        settings = make_settings(Path("."))
        registry = build_default_registry(settings)
        candidates = candidate_tool_specs("hi bud", registry.specs(), threshold=0.10, max_candidates=5)
        registry.close()

        self.assertEqual(candidates, [])

    def test_registry_prefilter_finds_tool_shaped_requests(self) -> None:
        settings = make_settings(Path("."))
        registry = build_default_registry(settings)
        specs = registry.specs()

        weather = candidate_tool_specs("will it rain in delhi today", specs, threshold=0.10, max_candidates=5)
        file_read = candidate_tool_specs("read README.md", specs, threshold=0.10, max_candidates=5)
        base64_encode = candidate_tool_specs("encode friday base64", specs, threshold=0.10, max_candidates=5)
        registry.close()

        self.assertIn("weather", {spec.name for spec in weather})
        self.assertIn("file_read", {spec.name for spec in file_read})
        self.assertIn("base64_encode", {spec.name for spec in base64_encode})

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
