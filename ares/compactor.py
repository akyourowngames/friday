"""Four-phase context compression following Hermes Agent pattern."""

from __future__ import annotations

import asyncio
import concurrent.futures
from typing import Any

from ares.context_blend import TokenEstimator, get_model_budgets


class ContextCompactor:
    """Compresses conversation history when it approaches context limits."""

    COMPACT_THRESHOLD = 0.90  # fallback if model budgets unavailable
    PROTECTED_TAIL_MESSAGES = 20
    HEAD_MESSAGES = 2
    PRUNE_TOOL_CHARS = 200

    def __init__(self, llm_client: Any, config: Any):
        self.llm = llm_client
        self.config = config
        self.estimator = TokenEstimator()

    def should_compact(self, history: list[dict]) -> bool:
        """Check if history exceeds model-aware compaction threshold."""
        if len(history) < 6:
            return False
        tokens = self.estimator.estimate_history(history)
        budgets = get_model_budgets(self.config.model)
        window = budgets["context_window"]
        threshold = budgets.get("compact_threshold", self.COMPACT_THRESHOLD)
        limit = int(window * threshold)
        return tokens > limit

    def compact(self, history: list[dict]) -> list[dict]:
        """Execute the four-phase compression algorithm."""
        if len(history) < 4:
            return history

        history = self._phase1_prune_tool_results(history)
        head, middle, tail = self._phase2_split(history)

        if not middle:
            return history

        summary = self._phase3_summarize(middle)
        return self._phase4_assemble(head, summary, tail)

    def _phase1_prune_tool_results(self, history: list[dict]) -> list[dict]:
        """Replace verbose tool outputs with short stubs."""
        protected = max(0, len(history) - self.PROTECTED_TAIL_MESSAGES)
        result = []
        for i, msg in enumerate(history):
            if i < protected and msg.get("role") == "tool":
                content = msg.get("content", "")
                if isinstance(content, str) and len(content) > self.PRUNE_TOOL_CHARS:
                    msg = dict(msg)
                    msg["content"] = content[: self.PRUNE_TOOL_CHARS] + f"\n... [tool output pruned, {len(content)} chars]"
            result.append(msg)
        return result

    def _phase2_split(self, history: list[dict]) -> tuple[list, list, list]:
        """Split history into head, middle, tail."""
        head = history[: self.HEAD_MESSAGES]
        tail = (
            history[-self.PROTECTED_TAIL_MESSAGES :]
            if len(history) > self.HEAD_MESSAGES + self.PROTECTED_TAIL_MESSAGES
            else []
        )
        middle = (
            history[self.HEAD_MESSAGES : -self.PROTECTED_TAIL_MESSAGES]
            if tail
            else history[self.HEAD_MESSAGES :]
        )
        return head, middle, tail

    def _phase3_summarize(self, middle: list[dict]) -> str:
        """Generate structured summary using LLM."""
        conversation_text = "\n".join(
            f"[{msg.get('role', 'unknown')}]: {msg.get('content', '')[:500]}"
            for msg in middle
            if msg.get("content")
        )

        summary_prompt = (
            "Write a concise structured summary of this conversation segment.\n"
            "Focus on what matters for continuing the work. Be specific, not vague.\n\n"
            f"Conversation:\n{conversation_text}\n\n"
            "Write your summary in this exact format:\n"
            "## Goals\n- What the user wanted to accomplish\n\n"
            "## Progress\n- What was completed (done)\n- What's in progress\n- What's blocked\n\n"
            "## Decisions\n- Key choices made and their rationale\n\n"
            "## Files Modified\n- List of files changed or discussed\n\n"
            "## Next Steps\n- What remains to be done\n\n"
            "Summary:"
        )

        try:
            result = self._call_llm_sync(summary_prompt)
            if result:
                return result
        except Exception:
            pass
        return self._fallback_summary(middle)

    def _call_llm_sync(self, prompt: str) -> str:
        """Call the LLM synchronously, handling async from sync context."""
        messages = [{"role": "user", "content": prompt}]

        async def _do():
            resp = await self.llm.chat(messages, tools=[])
            return resp.get("content", "") or ""

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            with concurrent.futures.ThreadPoolExecutor() as pool:
                return pool.submit(asyncio.run, _do()).result(timeout=30)
        elif loop:
            return loop.run_until_complete(_do())
        return asyncio.run(_do())

    def _fallback_summary(self, middle: list[dict]) -> str:
        """Simple fallback when LLM summarization fails."""
        topics = []
        for msg in middle:
            if msg.get("role") == "user" and msg.get("content"):
                topics.append(msg["content"][:100])
        return f"## Summary\nTopics discussed: {'; '.join(topics[:5])}"

    def _phase4_assemble(self, head: list, summary: str, tail: list) -> list[dict]:
        """Reassemble compressed context: head + summary message + tail."""
        summary_msg = {
            "role": "system",
            "content": f"[Previous conversation summary]\n{summary}",
            "_is_summary": True,
        }
        return head + [summary_msg] + tail

    def summarize_for_session(self, history: list[dict]) -> str:
        """Create a structured session summary (called at session end)."""
        conversation_text = "\n".join(
            f"[{msg.get('role', 'unknown')}]: {msg.get('content', '')[:800]}"
            for msg in history
            if msg.get("content") and msg.get("role") in ("user", "assistant")
        )

        if not conversation_text.strip():
            return ""

        prompt = (
            "Summarize this conversation session for future reference.\n"
            "Be concise but capture the key details that would help in a future session.\n\n"
            f"Conversation:\n{conversation_text}\n\n"
            "Write a structured summary (max 500 words):\n"
            "## What happened\nBrief description of the conversation.\n\n"
            "## Key decisions\n- Decisions made and why\n\n"
            "## Files/changes\n- Files discussed or modified\n\n"
            "## Open items\n- Things left undone or to follow up on\n\n"
            "Summary:"
        )

        try:
            result = self._call_llm_sync(prompt)
            return result or self._fallback_summary(history)
        except Exception:
            return self._fallback_summary(history)
