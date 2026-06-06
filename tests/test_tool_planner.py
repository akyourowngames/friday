from __future__ import annotations

import json
import unittest
from pathlib import Path

from assistant_cli.tool_planner import (
    candidate_tool_specs,
    parse_tool_plan,
    project_continuation_plan,
    repair_tool_plan_scope,
    tool_result_context,
)
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

    def test_repair_project_bulk_followup_scope(self) -> None:
        plan = parse_tool_plan(
            json.dumps(
                {
                    "tool": "project_manage",
                    "arguments": {"action": "task_complete", "project": "Friday", "task": "latency pass"},
                    "confidence": 0.85,
                }
            ),
            {"project_manage"},
        )

        repaired = repair_tool_plan_scope(plan, "mark them done")

        self.assertEqual(repaired.arguments["action"], "task_complete_all")
        self.assertTrue(repaired.arguments["all"])
        self.assertNotIn("task", repaired.arguments)
        self.assertEqual(repaired.arguments["project"], "Friday")

    def test_repair_project_task_create_title(self) -> None:
        plan = parse_tool_plan(
            json.dumps(
                {
                    "tool": "project_manage",
                    "arguments": {"action": "task_create", "project": "Friday"},
                    "confidence": 0.85,
                }
            ),
            {"project_manage"},
        )

        repaired = repair_tool_plan_scope(plan, "add a Friday task called latency pass")

        self.assertEqual(repaired.arguments["action"], "task_create")
        self.assertEqual(repaired.arguments["project"], "Friday")
        self.assertEqual(repaired.arguments["title"], "latency pass")

    def test_repair_keeps_task_create_request_from_becoming_completion(self) -> None:
        plan = parse_tool_plan(
            json.dumps(
                {
                    "tool": "project_manage",
                    "arguments": {"action": "task_complete_all", "project": "Friday"},
                    "confidence": 0.85,
                }
            ),
            {"project_manage"},
        )

        repaired = repair_tool_plan_scope(plan, "add a Friday task called final all check")

        self.assertEqual(repaired.arguments["action"], "task_create")
        self.assertEqual(repaired.arguments["title"], "final all check")
        self.assertEqual(repaired.arguments["project"], "Friday")
        self.assertNotIn("all", repaired.arguments)

    def test_repair_double_check_task_list_to_summary(self) -> None:
        plan = parse_tool_plan(
            json.dumps(
                {
                    "tool": "project_manage",
                    "arguments": {"action": "task_list", "project": "Friday"},
                    "confidence": 0.85,
                }
            ),
            {"project_manage"},
        )

        repaired = repair_tool_plan_scope(plan, "yeah double check pls")

        self.assertEqual(repaired.arguments["action"], "summary")
        self.assertEqual(repaired.arguments["project"], "Friday")

    def test_repair_pending_task_request_to_task_list(self) -> None:
        plan = parse_tool_plan(
            json.dumps(
                {
                    "tool": "project_manage",
                    "arguments": {"action": "project_list"},
                    "confidence": 0.85,
                }
            ),
            {"project_manage"},
        )
        records = [
            {
                "role": "tool",
                "tool": "project_manage",
                "content": json.dumps(
                    {
                        "tool": "project_manage",
                        "ok": True,
                        "data": {
                            "action": "task_create",
                            "task": {
                                "id": "task_recent",
                                "title": "latency pass",
                                "project_name": "Friday",
                                "status": "pending",
                            },
                        },
                    }
                ),
            }
        ]

        repaired = repair_tool_plan_scope(plan, "what tasks are pending for Friday", records)

        self.assertEqual(repaired.arguments["action"], "task_list")
        self.assertEqual(repaired.arguments["status"], "pending")
        self.assertEqual(repaired.arguments["project"], "Friday")

    def test_project_continuation_marks_recent_task_done(self) -> None:
        records = [
            {
                "role": "tool",
                "tool": "project_manage",
                "content": json.dumps(
                    {
                        "tool": "project_manage",
                        "ok": True,
                        "text": "Created task restart persistence check",
                        "data": {
                            "action": "task_create",
                            "task": {
                                "id": "task_recent",
                                "title": "restart persistence check",
                                "project_name": "Friday",
                                "status": "pending",
                            },
                        },
                    }
                ),
            }
        ]

        plan = project_continuation_plan("mark it done", records)

        self.assertIsNotNone(plan)
        assert plan is not None
        self.assertEqual(plan.arguments["action"], "task_complete")
        self.assertEqual(plan.arguments["task_id"], "task_recent")
        self.assertEqual(plan.arguments["project"], "Friday")

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
