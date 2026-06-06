from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from assistant_cli.tool_planner import (
    ToolPlanner,
    ToolPlan,
    PlannedToolCall,
    compact_recent_tool_results,
    no_tool_executed_context,
    tool_results_context,
)
from assistant_cli.tools import build_default_registry
from assistant_cli.tools.core import ToolResult
from test_tools import make_settings


class FakeChat:
    def __init__(
        self,
        settings,
        calls=None,
        error: Exception | None = None,
        review: dict | None = None,
        corrected_calls=None,
    ) -> None:
        self.settings = settings
        self.calls = calls or []
        self.corrected_calls = corrected_calls
        self.error = error
        self.review = review or {
            "intent": "mutation",
            "requested_operation": "task_create",
            "current_action_requested": True,
            "complete_project_definition": False,
            "bare_fragment": False,
            "calls_faithful": True,
            "references_present": False,
            "references_resolved": True,
            "set_reference_present": False,
            "coverage_complete": True,
            "reason": "matches current request",
        }
        self.received_tools = []
        self.received_messages = []
        self.auto_calls = 0

    def choose_tools(self, messages, tools, timeout, model=None, tool_choice="auto"):
        self.received_messages = messages
        self.received_tools = tools
        if self.error:
            raise self.error
        forced_name = (
            tool_choice.get("function", {}).get("name")
            if isinstance(tool_choice, dict)
            else ""
        )
        if forced_name == "submit_intent_review":
            return [
                {
                    "id": "review_1",
                    "tool": "submit_intent_review",
                    "arguments": self.review,
                }
            ]
        self.auto_calls += 1
        if (self.auto_calls > 1 or forced_name) and self.corrected_calls is not None:
            return self.corrected_calls
        return self.calls


class ToolPlannerTests(unittest.TestCase):
    def test_native_function_call_is_preserved_without_phrase_repairs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp))
            registry = build_default_registry(settings, Path(tmp))
            chat = FakeChat(
                settings,
                calls=[
                    {
                        "id": "call_1",
                        "tool": "task_create_many",
                        "arguments": {
                            "project": "Friday",
                            "tasks": [{"title": "Tool sanity"}, {"title": "JSONL audit"}],
                        },
                    }
                ],
            )

            plan = ToolPlanner(chat, registry).plan(
                "add those to this project",
                [
                    {"role": "assistant", "content": "Tool sanity and JSONL audit"},
                    {"role": "user", "content": "add those to this project"},
                ],
            )
            auto_schema_count = len(registry.openai_schemas(auto_only=True))
            registry.close()

        self.assertTrue(plan.uses_tool)
        self.assertEqual(plan.calls[0].tool, "task_create_many")
        self.assertEqual(len(plan.calls[0].arguments["tasks"]), 2)
        self.assertGreaterEqual(auto_schema_count, 20)

    def test_no_native_call_stays_no_tool(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp))
            registry = build_default_registry(settings, Path(tmp))
            plan = ToolPlanner(FakeChat(settings), registry).plan(
                "hi bud",
                [{"role": "user", "content": "hi bud"}],
            )
            registry.close()

        self.assertFalse(plan.uses_tool)
        self.assertEqual(plan.error, "")
        self.assertIn("No registered tool was executed", no_tool_executed_context(plan))

    def test_explicit_create_action_is_not_blocked_by_imperfect_intent_label(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp))
            registry = build_default_registry(settings, Path(tmp))
            chat = FakeChat(
                settings,
                calls=[
                    {
                        "id": "create_1",
                        "tool": "project_create",
                        "arguments": {"name": "Friday"},
                    }
                ],
                review={
                    "intent": "project_definition",
                    "requested_operation": "project_create",
                    "current_action_requested": True,
                    "complete_project_definition": False,
                    "bare_fragment": False,
                    "calls_faithful": True,
                    "references_present": False,
                    "references_resolved": True,
                    "set_reference_present": False,
                    "coverage_complete": True,
                    "reason": "The latest turn explicitly asks to create the project.",
                },
            )

            plan = ToolPlanner(chat, registry).plan(
                "create a project called Friday",
                [{"role": "user", "content": "create a project called Friday"}],
            )
            registry.close()

        self.assertTrue(plan.uses_tool)
        self.assertEqual(plan.calls[0].tool, "project_create")

    def test_rejected_project_definition_is_replanned_as_project_update(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp))
            registry = build_default_registry(settings, Path(tmp))
            chat = FakeChat(
                settings,
                calls=[
                    {
                        "id": "bad_1",
                        "tool": "task_create",
                        "arguments": {"project": "Friday", "title": "this is my AI assistant"},
                    }
                ],
                corrected_calls=[
                    {
                        "id": "good_1",
                        "tool": "project_update",
                        "arguments": {"project": "Friday", "description": "My AI assistant"},
                    }
                ],
                review={
                    "intent": "project_definition",
                    "requested_operation": "project_update",
                    "current_action_requested": False,
                    "complete_project_definition": True,
                    "bare_fragment": False,
                    "calls_faithful": True,
                    "references_present": True,
                    "references_resolved": True,
                    "set_reference_present": False,
                    "coverage_complete": True,
                    "reason": "This defines the current project.",
                },
            )

            plan = ToolPlanner(chat, registry).plan(
                "add yourself as this is my AI assistant",
                [
                    {"role": "assistant", "content": "Project Friday was created."},
                    {"role": "user", "content": "add yourself as this is my AI assistant"},
                ],
            )
            registry.close()

        self.assertTrue(plan.uses_tool)
        self.assertEqual(plan.calls[0].tool, "project_update")
        self.assertEqual(chat.auto_calls, 2)

    def test_misrouted_project_creation_is_forced_back_to_project_create(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp))
            registry = build_default_registry(settings, Path(tmp))
            chat = FakeChat(
                settings,
                calls=[
                    {
                        "id": "bad_1",
                        "tool": "project_update",
                        "arguments": {"project": "Friday"},
                    }
                ],
                corrected_calls=[
                    {
                        "id": "good_1",
                        "tool": "project_create",
                        "arguments": {"name": "Friday"},
                    }
                ],
                review={
                    "intent": "project_definition",
                    "requested_operation": "project_create",
                    "current_action_requested": True,
                    "complete_project_definition": True,
                    "bare_fragment": False,
                    "calls_faithful": False,
                    "references_present": False,
                    "references_resolved": True,
                    "set_reference_present": False,
                    "coverage_complete": True,
                    "reason": "The requested operation is project creation.",
                },
            )

            plan = ToolPlanner(chat, registry).plan(
                "create a project called Friday",
                [{"role": "user", "content": "create a project called Friday"}],
            )
            registry.close()

        self.assertTrue(plan.uses_tool)
        self.assertEqual(plan.calls[0].tool, "project_create")

    def test_bare_topic_is_not_persisted_as_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp))
            registry = build_default_registry(settings, Path(tmp))
            chat = FakeChat(
                settings,
                calls=[
                    {
                        "id": "bad_1",
                        "tool": "task_create",
                        "arguments": {"project": "Friday", "title": "automation"},
                    }
                ],
                corrected_calls=[],
                review={
                    "intent": "brainstorming",
                    "requested_operation": "none",
                    "current_action_requested": False,
                    "complete_project_definition": False,
                    "bare_fragment": True,
                    "calls_faithful": False,
                    "references_present": False,
                    "references_resolved": False,
                    "set_reference_present": False,
                    "coverage_complete": False,
                    "reason": "A bare topic is not a persistence request.",
                },
            )

            plan = ToolPlanner(chat, registry).plan(
                "automation",
                [
                    {"role": "assistant", "content": "What is the first move?"},
                    {"role": "user", "content": "automation"},
                ],
            )
            registry.close()

        self.assertFalse(plan.uses_tool)
        self.assertIn("bare topic", plan.rejection.lower())

    def test_incomplete_referenced_batch_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp))
            registry = build_default_registry(settings, Path(tmp))
            chat = FakeChat(
                settings,
                calls=[
                    {
                        "id": "bad_1",
                        "tool": "task_create_many",
                        "arguments": {"project": "Friday", "tasks": [{"title": "Tool sanity"}]},
                    }
                ],
                corrected_calls=[],
                review={
                    "intent": "mutation",
                    "requested_operation": "task_create",
                    "current_action_requested": True,
                    "complete_project_definition": False,
                    "bare_fragment": False,
                    "calls_faithful": False,
                    "references_present": True,
                    "references_resolved": True,
                    "set_reference_present": True,
                    "coverage_complete": False,
                    "reason": "The proposed batch drops referenced items.",
                },
            )

            plan = ToolPlanner(chat, registry).plan(
                "add them to Friday",
                [
                    {
                        "role": "assistant",
                        "content": "Tool sanity, persistence check, latency pass, and JSONL audit.",
                    },
                    {"role": "user", "content": "add them to Friday"},
                ],
            )
            registry.close()

        self.assertFalse(plan.uses_tool)
        self.assertIn("drops referenced items", plan.rejection.lower())

    def test_project_scoped_bulk_completion_does_not_require_enumerated_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp))
            registry = build_default_registry(settings, Path(tmp))
            chat = FakeChat(
                settings,
                calls=[
                    {
                        "id": "bulk_1",
                        "tool": "task_complete_all",
                        "arguments": {"project": "Friday"},
                    }
                ],
                review={
                    "intent": "mutation",
                    "requested_operation": "task_update",
                    "current_action_requested": True,
                    "complete_project_definition": False,
                    "bare_fragment": False,
                    "calls_faithful": False,
                    "references_present": False,
                    "references_resolved": False,
                    "set_reference_present": True,
                    "coverage_complete": False,
                    "reason": "Recent conversation does not enumerate every task.",
                },
            )

            plan = ToolPlanner(chat, registry).plan(
                "mark all remaining Friday tasks done",
                [{"role": "user", "content": "mark all remaining Friday tasks done"}],
            )
            registry.close()

        self.assertTrue(plan.uses_tool)
        self.assertEqual(plan.calls[0].tool, "task_complete_all")

    def test_planner_error_blocks_unproven_tool_claims(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp))
            registry = build_default_registry(settings, Path(tmp))
            plan = ToolPlanner(FakeChat(settings, error=TimeoutError("planner timeout")), registry).plan(
                "create project Friday",
                [{"role": "user", "content": "create project Friday"}],
            )
            registry.close()

        self.assertFalse(plan.uses_tool)
        self.assertIn("TimeoutError", plan.error)
        context = no_tool_executed_context(plan)
        self.assertIn("was not performed", context)
        self.assertIn("Do not claim", context)

    def test_recent_tool_results_are_compacted_for_reference_resolution(self) -> None:
        records = [
            {
                "role": "tool",
                "tool": "project_manage",
                "content": json.dumps(
                    {
                        "tool": "project_manage",
                        "ok": True,
                        "text": "Created project Friday",
                        "data": {
                            "action": "project_create",
                            "project": {"id": "proj_1", "name": "Friday", "status": "in_progress"},
                        },
                    }
                ),
            }
        ]

        compacted = compact_recent_tool_results(records)

        self.assertEqual(compacted[0]["tool"], "project_manage")
        self.assertEqual(compacted[0]["data"]["project"]["name"], "Friday")

    def test_tool_results_context_is_authoritative_for_multiple_calls(self) -> None:
        plan = ToolPlan(
            calls=(
                PlannedToolCall("project_manage", {"action": "project_create", "name": "Friday"}),
                PlannedToolCall("project_manage", {"action": "task_create", "project": "Friday", "title": "Audit"}),
            )
        )
        results = [
            ToolResult("project_manage", True, "Created project Friday", {"action": "project_create"}),
            ToolResult("project_manage", True, "Created task Audit", {"action": "task_create"}),
        ]

        context = tool_results_context(plan, results, max_chars=3000)

        self.assertIn("AUTHORITATIVE SOURCE OF TRUTH", context)
        self.assertIn("Created project Friday", context)
        self.assertIn("Created task Audit", context)


if __name__ == "__main__":
    unittest.main()
