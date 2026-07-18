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
    """Always return None - all memories are accepted (guardrails removed)."""
    return None
