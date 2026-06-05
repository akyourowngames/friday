from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from assistant_cli.langchain_memory import JsonlChatMessageHistory


class JsonlChatMessageHistoryTests(unittest.TestCase):
    def test_tool_message_is_saved_but_not_sent_as_chat_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = JsonlChatMessageHistory(str(Path(tmp)))
            history.add_user_message("whats weather in delhi")
            history.add_tool_message("weather", '{"ok": true}')
            history.add_ai_message("Delhi is warm.")

            records = history.records()
            recent = history.recent_openai_messages(20)

        self.assertEqual([record["role"] for record in records], ["user", "tool", "assistant"])
        self.assertEqual(records[1]["tool"], "weather")
        self.assertEqual([message["role"] for message in recent], ["user", "assistant"])


if __name__ == "__main__":
    unittest.main()
