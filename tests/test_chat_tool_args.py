from __future__ import annotations

import unittest

from chat import no_tool_executed_context, parse_tool_args
from assistant_cli.tools.core import ToolSpec, schema


class ChatToolArgsTests(unittest.TestCase):
    def test_parse_tool_args_accepts_remainder_tokens_with_spaces(self) -> None:
        args = parse_tool_args(
            [
                "action=task_create",
                "project=Friday",
                "title=fix voice latency",
                "priority=high",
                "due=tomorrow",
            ]
        )

        self.assertEqual(args["action"], "task_create")
        self.assertEqual(args["project"], "Friday")
        self.assertEqual(args["title"], "fix voice latency")
        self.assertEqual(args["priority"], "high")
        self.assertEqual(args["due"], "tomorrow")

    def test_parse_tool_args_still_accepts_single_shell_string(self) -> None:
        args = parse_tool_args('action=task_create project=Friday title="fix voice latency"')

        self.assertEqual(args["title"], "fix voice latency")

    def test_no_tool_context_blocks_unproven_mutation_claims(self) -> None:
        context = no_tool_executed_context(
            [
                ToolSpec(
                    name="project_manage",
                    description="Manage projects",
                    parameters=schema({}),
                )
            ]
        )

        self.assertIn("no tool was executed", context)
        self.assertIn("no change or verification", context)
        self.assertIn("project_manage", context)


if __name__ == "__main__":
    unittest.main()
