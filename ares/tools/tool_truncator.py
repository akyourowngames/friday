"""Tool output truncation — keeps context lean by trimming large tool results."""

from __future__ import annotations


class ToolTruncator:
    """Truncates large tool output messages in conversation history."""

    MAX_TOOL_OUTPUT_CHARS = 500
    TRUNCATED_HEAD = 150
    TRUNCATED_TAIL = 100
    STUB_TEMPLATE = (
        "{head}\n\n... [truncated — {original_len} chars total, "
        "{remaining} remaining] ...\n\n{tail}"
    )

    def __init__(self, max_chars: int | None = None):
        if max_chars is not None:
            self.MAX_TOOL_OUTPUT_CHARS = max_chars

    def truncate_history(self, history: list[dict]) -> list[dict]:
        """Truncate large tool outputs in old messages. Never truncate recent messages."""
        protected = max(0, len(history) - 6)
        result = []
        for i, msg in enumerate(history):
            if i < protected and msg.get("role") == "tool":
                content = msg.get("content", "")
                if isinstance(content, str) and len(content) > self.MAX_TOOL_OUTPUT_CHARS:
                    msg = dict(msg)
                    msg["content"] = self._truncate_output(content)
                    msg["_truncated"] = True
            result.append(msg)
        return result

    def _truncate_output(self, content: str) -> str:
        """Truncate a tool output, keeping head and tail."""
        original_len = len(content)
        head = content[: self.TRUNCATED_HEAD]
        tail = (
            content[-self.TRUNCATED_TAIL :]
            if original_len > self.TRUNCATED_HEAD + self.TRUNCATED_TAIL
            else ""
        )
        return self.STUB_TEMPLATE.format(
            head=head,
            tail=tail,
            original_len=original_len,
            remaining=original_len - self.TRUNCATED_HEAD - len(tail),
        )

    def truncate_single(self, content: str, role: str = "tool") -> str:
        """Truncate a single tool result before adding to history."""
        if role != "tool" or len(content) <= self.MAX_TOOL_OUTPUT_CHARS:
            return content
        return self._truncate_output(content)
