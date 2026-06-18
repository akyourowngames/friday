"""Token estimation, truncation, and context blending utilities."""

from __future__ import annotations


def estimate_tokens(text: str) -> int:
    """Return a rough token estimate for context budgeting."""
    if not text.strip():
        return 0
    return max(1, int(len(text.split()) * 1.3))


def truncate_to_tokens(text: str, max_tokens: int) -> str:
    """Truncate text to fit a rough token budget."""
    if max_tokens <= 0:
        return ""
    words = text.split()
    max_words = max(1, int(max_tokens / 1.3))
    if len(words) <= max_words:
        return text
    return " ".join(words[:max_words]) + "\n\n<!-- truncated to fit context budget -->"


def format_memories(memories: list[dict] | None, token_budget: int = 800) -> str:
    """Format retrieved memories for context injection."""
    if not memories:
        return ""
    lines = ["## What I know about you:"]
    for memory in memories:
        cat = memory.get("category", "note")
        importance = memory.get("importance", 0.5)
        fact_id = memory.get("fact_id", "?")
        fact_text = memory.get("fact_text") or memory.get("content") or ""
        if fact_text:
            lines.append(f"- [{cat}, importance={importance}] #{fact_id}: {fact_text}")
    return truncate_to_tokens("\n".join(lines), token_budget)


def format_tasks(tasks: list[dict] | None) -> str:
    """Format pending tasks for context injection."""
    if not tasks:
        return ""
    lines = ["## Your pending tasks:"]
    for task in tasks[:5]:
        due = f" (due: {task['due']})" if task.get("due") else ""
        lines.append(f"- {task['title']}{due}")
    return "\n".join(lines)


def format_summaries(summaries: list[str] | None) -> str:
    """Format recent conversation summaries for context injection."""
    if not summaries:
        return ""
    lines = ["## Recent session summaries:"]
    for summary in summaries:
        if summary:
            lines.append(f"- {summary}")
    return "\n".join(lines)


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
    tasks: list[dict] | None = None,
    conversation_summaries: list[str] | None = None,
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

    summary_text = format_summaries(conversation_summaries)
    remaining = _append_section(sections, summary_text, remaining)

    if memories and remaining > 100:
        memory_section = format_memories(memories, token_budget=remaining)
        remaining = _append_section(sections, memory_section, remaining)

    task_section = format_tasks(tasks)
    if task_section:
        sections.append(task_section)

    return "\n\n".join(sections)
