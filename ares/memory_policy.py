"""Deterministic guardrails for what may become long-term memory."""

from __future__ import annotations

import re


LOW_CONFIDENCE_THRESHOLD = 0.5

_INSULT_RE = re.compile(
    r"\b(fuck you|shut up|stupid|idiot|dumb|useless|trash|terrible)\b",
    re.IGNORECASE,
)

_TEMPORARY_STATE_RE = re.compile(
    r"\b("
    r"today|tonight|right now|currently|this session|this conversation|"
    r"weather|temperature|forecast|rain|storm|date is|time is|clock says|"
    r"logged in|logged out|login page|browser is|screenshot|"
    r"tool output|command output|phone bridge|playwright|mcp"
    r")\b",
    re.IGNORECASE,
)

_TASK_REQUEST_RE = re.compile(
    r"\b(user asked|user wants|user requested|user is asking|user told ares|"
    r"open instagram|check instagram|check my insta|run command|execute)\b",
    re.IGNORECASE,
)


def memory_rejection_reason(
    fact_text: str,
    *,
    category: str = "note",
    confidence: float = 1.0,
) -> str | None:
    """Return why a memory should be rejected, or None if it is acceptable.

    This is intentionally conservative. It blocks obvious non-durable memories
    while leaving nuanced judgment to the model and the user.
    """
    text = re.sub(r"\s+", " ", (fact_text or "")).strip()
    lowered = text.lower()
    category = (category or "note").lower()

    if len(text) < 4:
        return "memory is too short"
    if confidence < LOW_CONFIDENCE_THRESHOLD:
        return "memory confidence is too low"
    if category not in {"preference", "fact", "belief", "habit", "relationship", "project", "plan", "note"}:
        return "memory category is invalid"
    if _INSULT_RE.search(lowered) and not re.search(r"\b(prefers|likes|dislikes|avoids)\b", lowered):
        return "memory looks like a transient insult or mood"
    if _TASK_REQUEST_RE.search(lowered):
        return "memory looks like a temporary request"
    if _TEMPORARY_STATE_RE.search(lowered):
        return "memory looks like temporary state or tool output"

    return None
