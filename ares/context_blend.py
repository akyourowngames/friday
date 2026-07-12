"""Token estimation, truncation, and context blending utilities."""

from __future__ import annotations

import re

CONTEXT_WINDOWS: dict[str, int] = {
    # OpenCode free tier
    "deepseek-v4-flash-free": 128_000,
    "deepseek-v4-flash": 128_000,
    "deepseek-v4-pro": 128_000,
    "mimo-v2.5-free": 128_000,
    "qwen3.6-plus-free": 128_000,
    "qwen3.6-plus": 128_000,
    "minimax-m3-free": 128_000,
    "nemotron-3-ultra-free": 128_000,
    "north-mini-code-free": 128_000,
    "big-pickle": 128_000,
    # Claude
    "claude-fable-5": 200_000,
    "claude-opus-4-8": 200_000,
    "claude-opus-4-7": 200_000,
    "claude-opus-4-6": 200_000,
    "claude-opus-4-5": 200_000,
    "claude-opus-4-1": 200_000,
    "claude-sonnet-4-6": 200_000,
    "claude-sonnet-4-5": 200_000,
    "claude-sonnet-4": 200_000,
    "claude-haiku-4-5": 200_000,
    # GPT
    "gpt-5.5": 128_000,
    "gpt-5.5-pro": 128_000,
    "gpt-5.4": 128_000,
    "gpt-5.4-pro": 128_000,
    "gpt-5.4-mini": 128_000,
    "gpt-5.4-nano": 128_000,
    "gpt-5.3-codex-spark": 128_000,
    "gpt-5.3-codex": 128_000,
    "gpt-5.2": 128_000,
    "gpt-5.2-codex": 128_000,
    "gpt-5.1": 128_000,
    "gpt-5.1-codex-max": 128_000,
    "gpt-5.1-codex": 128_000,
    "gpt-5.1-codex-mini": 128_000,
    "gpt-5": 128_000,
    "gpt-5-codex": 128_000,
    "gpt-5-nano": 128_000,
    "gpt-4o": 128_000,
    "gpt-4o-mini": 128_000,
    # Gemini (large context)
    "gemini-3.5-flash": 1_000_000,
    "gemini-3-flash": 1_000_000,
    "gemini-3.1-pro": 1_000_000,
    # Grok
    "grok-build-0.1": 128_000,
    # GLM
    "glm-5.1": 128_000,
    "glm-5": 128_000,
    # Moonshot
    "kimi-k2.6": 128_000,
    "kimi-k2.5": 128_000,
    # MiniMax
    "minimax-m2.7": 128_000,
    "minimax-m2.5": 128_000,
    # Qwen
    "qwen3.5-plus": 128_000,
}

# Default context window for unknown models
DEFAULT_CONTEXT_WINDOW = 128_000
_EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
_PHONE_RE = re.compile(r"(?<!\w)(?:\+?\d[\d() .-]{6,}\d)(?!\w)")


def get_model_budgets(model: str) -> dict[str, int]:
    """Scale context budgets based on model's context window size.

    Following Cline's pattern: smaller models get tighter budgets,
    larger models get proportionally more resources.
    """
    window = CONTEXT_WINDOWS.get(model, DEFAULT_CONTEXT_WINDOW)

    # Buffer: reserve for output tokens (model generates responses)
    # Cline formula: max(contextWindow - 40_000, contextWindow * 0.8)
    buffer = max(window - 40_000, int(window * 0.8))
    usable = window - buffer

    # Scale system prompt budget (soul + profile + project + memories)
    # Small models (128k): ~4k tokens for context
    # Medium models (200k): ~8k tokens
    # Large models (1M+): ~32k tokens
    if window >= 500_000:
        context_token_budget = 32_000
        max_memory_retrieval = 30
    elif window >= 200_000:
        context_token_budget = 8_000
        max_memory_retrieval = 15
    elif window >= 128_000:
        context_token_budget = 4_000
        max_memory_retrieval = 8
    else:
        context_token_budget = 2_000
        max_memory_retrieval = 5

    # Compaction threshold: compact when history exceeds 80% of usable window
    compact_threshold = 0.80

    # Max context messages: scale with window size
    max_context_messages = min(max(window // 4_000, 10), 100)

    return {
        "context_window": window,
        "usable_window": usable,
        "buffer": buffer,
        "context_token_budget": context_token_budget,
        "max_memory_retrieval": max_memory_retrieval,
        "compact_threshold": compact_threshold,
        "max_context_messages": max_context_messages,
    }


def estimate_token_breakdown(
    system_prompt: str,
    history: list[dict],
    tool_outputs: list[str] | None = None,
) -> dict[str, int]:
    """Estimate token breakdown for context bar display.

    Returns dict with:
      - system_prompt: tokens in blended system context
      - history: tokens in conversation messages
      - tool_output: tokens in tool call results
      - total: sum of all parts
    """
    est = TokenEstimator()
    sys_tokens = est.estimate_text(system_prompt)
    hist_tokens = est.estimate_history(history)
    tool_tokens = sum(est.estimate_text(t, "tool_output") for t in (tool_outputs or []))
    return {
        "system_prompt": sys_tokens,
        "history": hist_tokens,
        "tool_output": tool_tokens,
        "total": sys_tokens + hist_tokens + tool_tokens,
    }


class TokenEstimator:
    """Estimates token counts for messages and content types."""

    WORD_RATIO = 1.3
    CODE_RATIO = 1.5
    TOOL_OUTPUT_RATIO = 1.8

    def estimate_text(self, text: str, content_type: str = "text") -> int:
        """Estimate tokens in a text string."""
        if not text.strip():
            return 0
        ratio = {
            "text": self.WORD_RATIO,
            "code": self.CODE_RATIO,
            "tool_output": self.TOOL_OUTPUT_RATIO,
        }.get(content_type, self.WORD_RATIO)
        return max(1, int(len(text.split()) * ratio))

    def estimate_message(self, msg: dict) -> int:
        """Estimate tokens in a single message dict."""
        content = msg.get("content", "")
        if isinstance(content, list):
            content = " ".join(
                str(part.get("text", "")) if isinstance(part, dict) else str(part)
                for part in content
            )
        if not isinstance(content, str):
            content = str(content)
        content_type = "tool_output" if msg.get("role") == "tool" else "text"
        tokens = self.estimate_text(content, content_type)
        if msg.get("tool_calls"):
            tokens += len(msg["tool_calls"]) * 50
        return tokens + 4

    def estimate_history(self, history: list[dict]) -> int:
        """Estimate total tokens in conversation history."""
        return sum(self.estimate_message(msg) for msg in history)

    def estimate_context_window(self, model: str) -> int:
        """Return the context window size for a model."""
        return CONTEXT_WINDOWS.get(model, DEFAULT_CONTEXT_WINDOW)

    def truncate_to_tokens(self, text: str, max_tokens: int) -> str:
        """Truncate text to fit a rough token budget."""
        if max_tokens <= 0:
            return ""
        words = text.split()
        max_words = max(1, int(max_tokens / self.WORD_RATIO))
        if len(words) <= max_words:
            return text
        return " ".join(words[:max_words]) + "\n\n<!-- truncated to fit context budget -->"


_estimator = TokenEstimator()


def estimate_tokens(text: str) -> int:
    """Return a rough token estimate for context budgeting."""
    return _estimator.estimate_text(text)


def truncate_to_tokens(text: str, max_tokens: int) -> str:
    """Truncate text to fit a rough token budget."""
    return _estimator.truncate_to_tokens(text, max_tokens)


def format_memories(memories: list[dict] | None, token_budget: int = 800) -> str:
    """Format retrieved memories for context injection."""
    if not memories:
        return ""
    lines = [
        "## What I know about you:",
        "Memory is for durable user-specific facts only. If a memory conflicts with runtime/tool evidence, trust the evidence and ask before updating memory.",
    ]
    for memory in memories:
        cat = memory.get("category", "note")
        importance = memory.get("importance", 0.5)
        fact_id = memory.get("fact_id", "?")
        fact_text = memory.get("fact_text") or memory.get("content") or ""
        if fact_text:
            lines.append(f"- [{cat}, importance={importance}] #{fact_id}: {fact_text}")
    return truncate_to_tokens("\n".join(lines), token_budget)


def format_people(people: list[dict] | None, token_budget: int = 500) -> str:
    """Format contact-redacted people context for the model.

    Names, relations, aliases, and the fact that a contact method exists are
    enough to prompt local alias resolution.  Phone/email values, notes, and
    dates must never be injected into model context.
    """
    if not people:
        return ""
    lines = [
        "## People & Relationships:",
        "These are explicitly saved local relationship records. Contact values stay local; use search_person or a supported action with an exact alias when needed. Never infer or auto-save a person from messages, contacts, or notifications.",
    ]
    for person in people:
        name = str(person.get("canonical_name") or "").strip()
        if not name:
            continue
        details: list[str] = []
        if person.get("relation"):
            details.append(str(person["relation"]))
        aliases = [str(alias) for alias in person.get("aliases", []) if str(alias).strip()]
        if aliases:
            details.append("aliases: " + ", ".join(aliases[:4]))
        availability = []
        if person.get("has_phone"):
            availability.append("phone available")
        if person.get("has_email"):
            availability.append("email available")
        if availability:
            details.append(", ".join(availability))
        if person.get("last_contacted_at"):
            channel = str(person.get("last_contacted_via") or "contact method")
            details.append(f"last contacted via {channel} at {person['last_contacted_at']}")
        suffix = f" ({'; '.join(details)})" if details else ""
        lines.append(f"- #{person.get('person_id', '?')}: {name}{suffix}")
    return truncate_to_tokens("\n".join(lines), token_budget)


def format_actions(actions: list[dict] | None, *, title: str, token_budget: int = 500) -> str:
    """Format bounded provenance entries; action records intentionally lack bodies."""
    if not actions:
        return ""
    lines = [f"## {title}:", "Action history is provenance, not message/content storage."]
    for action in actions:
        summary = str(action.get("summary") or "").strip()
        if not summary:
            continue
        created = str(action.get("created_at") or "")
        action_type = str(action.get("action_type") or "action")
        target = str(action.get("target") or "").strip()
        target_text = f" — {target}" if target else ""
        lines.append(f"- {created} [{action_type}] {summary}{target_text}")
    return truncate_to_tokens("\n".join(lines), token_budget)


def format_summaries(summaries: list[str] | None) -> str:
    """Format recent conversation summaries for context injection."""
    if not summaries:
        return ""
    lines = ["## Recent session summaries:"]
    for summary in summaries:
        if summary:
            lines.append(f"- {_redact_recall_text(summary, maximum=700)}")
    return "\n".join(lines)


def _redact_recall_text(value: object, *, maximum: int = 420) -> str:
    """Keep recalled local text useful while never injecting contact values."""
    text = " ".join(str(value or "").split())
    text = _EMAIL_RE.sub("[redacted email]", text)
    text = _PHONE_RE.sub("[redacted phone]", text)
    if len(text) > maximum:
        text = text[: maximum - 3].rstrip() + "..."
    return text


def format_conversation_recall(records: list[dict] | None, token_budget: int = 600) -> str:
    """Format bounded prior-chat evidence for an explicit continuation request."""
    if not records:
        return ""
    lines = [
        "## Relevant Prior Conversation (local recall):",
        "Use this only to continue the user's explicit reference. It is historical context, not live external state; contact values are redacted.",
    ]
    for record in records:
        content = _redact_recall_text(record.get("content"))
        if not content:
            continue
        role = str(record.get("role") or "message")
        created = str(record.get("created_at") or "")
        lines.append(f"- {created} [{role}] {content}")
    return truncate_to_tokens("\n".join(lines), token_budget)


def _append_section(sections: list[str], section: str, remaining: int) -> int:
    if not section or remaining <= 0:
        return remaining
    bounded = truncate_to_tokens(section, remaining)
    if bounded:
        sections.append(bounded)
        remaining -= estimate_tokens(bounded)
    return remaining


def build_context_prompt(
    soul_context: str = "",
    profile_context: str = "",
    project_context: str = "",
    memories: list[dict] | None = None,
    people: list[dict] | None = None,
    recent_actions: list[dict] | None = None,
    relevant_actions: list[dict] | None = None,
    recent_file_actions: list[dict] | None = None,
    conversation_summaries: list[str] | None = None,
    conversation_recall: list[dict] | None = None,
    previous_session_summary: str | None = None,
    token_budget: int = 2000,
) -> str:
    """Build a priority-ordered context string within a shared token budget."""
    if token_budget <= 0:
        return ""

    sections: list[str] = []
    remaining = token_budget

    remaining = _append_section(sections, soul_context, remaining)
    remaining = _append_section(sections, profile_context, remaining)
    remaining = _append_section(sections, project_context, remaining)

    # Inject previous session summary (high priority — recent context)
    if previous_session_summary and remaining > 0:
        summary_section = f"## Previous Session Summary\n{_redact_recall_text(previous_session_summary, maximum=900)}"
        remaining = _append_section(sections, summary_section, remaining)

    summary_text = format_summaries(conversation_summaries)
    remaining = _append_section(sections, summary_text, remaining)

    recall_text = format_conversation_recall(conversation_recall, token_budget=remaining)
    remaining = _append_section(sections, recall_text, remaining)

    if memories and remaining > 100:
        memory_section = format_memories(memories, token_budget=remaining)
        remaining = _append_section(sections, memory_section, remaining)

    if people and remaining > 100:
        people_section = format_people(people, token_budget=remaining)
        remaining = _append_section(sections, people_section, remaining)

    if recent_file_actions and remaining > 100:
        file_section = format_actions(recent_file_actions, title="Recent Files & Assets", token_budget=remaining)
        remaining = _append_section(sections, file_section, remaining)

    if relevant_actions and remaining > 100:
        relevant_section = format_actions(relevant_actions, title="Relevant Action History", token_budget=remaining)
        remaining = _append_section(sections, relevant_section, remaining)

    if recent_actions and remaining > 100:
        relevant_ids = {action.get("action_id") for action in relevant_actions or []}
        nonduplicated = [action for action in recent_actions if action.get("action_id") not in relevant_ids]
        action_section = format_actions(nonduplicated, title="Recent Actions", token_budget=remaining)
        remaining = _append_section(sections, action_section, remaining)

    return "\n\n".join(sections)
