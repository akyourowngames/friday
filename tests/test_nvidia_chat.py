from __future__ import annotations

import unittest

from assistant_cli.nvidia_chat import NvidiaChat
from assistant_cli.tools.core import ToolResult


class _Settings:
    last_messages = 20
    tool_response_model = "response-model"
    tool_verifier_fallback_model = "fallback-model"
    tool_planner_timeout_seconds = 1.0


class NvidiaChatMessageTests(unittest.TestCase):
    def test_current_turn_context_precedes_conversation_and_user_stays_last(self) -> None:
        chat = object.__new__(NvidiaChat)
        chat.settings = _Settings()
        chat.system_prompt = "Friday persona"
        chat.messages = [{"role": "system", "content": "Friday persona"}]

        messages = chat._messages_for_request(
            "No tool was executed. Do not claim success.",
            [
                {"role": "assistant", "content": "What should we do?"},
                {"role": "user", "content": "create it"},
            ],
        )

        self.assertEqual(messages[0]["role"], "system")
        self.assertIn("No tool was executed", messages[0]["content"])
        self.assertEqual(messages[-1], {"role": "user", "content": "create it"})

    def test_grounded_reply_rejects_inverted_success_claim(self) -> None:
        chat = object.__new__(NvidiaChat)
        chat.settings = _Settings()
        chat.system_prompt = "Friday persona"
        chat.choose_tools = lambda *args, **kwargs: [
            {
                "tool": "submit_grounded_response",
                "arguments": {
                    "response": "Project Friday created.",
                    "reports_success": True,
                    "reports_failure": False,
                    "reports_new_creation": True,
                    "reports_new_update": False,
                },
            }
        ]
        result = ToolResult("project_update", False, "Project not found: Friday", {"action": "project_update"})

        response = chat.grounded_reply("authoritative context", "create Friday", [result])

        self.assertEqual(response, "Project not found: Friday")


if __name__ == "__main__":
    unittest.main()
