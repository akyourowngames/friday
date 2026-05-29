"""Life Timeline: narrative episodes from memory.

Surfaces the cognition episode stitcher to the chat. Instead of dumping flat
facts, it returns the user's memories grouped into time-ordered episodes with
one-line titles, so KING can recall arcs ("the JEE-prep stretch") rather than
rows. Read-only over the live Brain.
"""

import time

from tools.registry import tool
from tools.runtime import (
    coerce_bool,
    emit_trace,
    error_payload,
    make_trace,
    normalize_int,
    normalize_response_format,
    structured_error,
    structured_success,
    utc_now_iso,
)

_TIMELINE_VERSION = "1.0.0"


def _trace(name, started_at, started, schema_valid, status, output_fields, error_code=None):
    return make_trace(
        name, _TIMELINE_VERSION, started_at, started, 1, schema_valid,
        "life_timeline", status, output_fields, {"count": 1, "systems": ["memory_brain", "cognition_episodes"]}, error_code,
    )


def _emit(name, started, started_at, trace_enabled, result=None, error=None, response_format="legacy", legacy="", status="SUCCESS"):
    valid = error is None
    trace = _trace(name, started_at, started, valid, status if valid else "FAILED",
                   len(result) if result else 1, None if valid else error["code"])
    emit_trace(trace, trace_enabled)
    if response_format == "structured":
        if valid:
            return structured_success(name, _TIMELINE_VERSION, result, started, trace)
        return structured_error(name, _TIMELINE_VERSION, error, started, trace)
    return legacy


@tool(
    name="life_timeline",
    description="Recall the user's memories as time-ordered narrative episodes (arcs), not flat facts. Useful for 'what was going on around then' or summarizing recent life.",
    examples=[
        "give me a timeline of what you remember",
        "what have I been up to lately",
        "summarize my recent episodes",
    ],
    param_descriptions={
        "limit": "Maximum episodes to return, 1 to 30",
        "response_format": "legacy or structured",
        "trace_enabled": "When true, emit a machine-readable trace entry",
    },
)
def life_timeline(limit: int = 8, response_format: str = "legacy", trace_enabled: bool = False):
    started = time.perf_counter()
    started_at = utc_now_iso()
    response_format = normalize_response_format(response_format)
    trace_enabled = coerce_bool(trace_enabled)
    limit, limit_err = normalize_int(limit, "limit", 8, 1, 30, "Use limit between 1 and 30.", "INVALID_LIMIT")
    if limit_err is not None:
        return _emit("life_timeline", started, started_at, trace_enabled, error=limit_err, response_format=response_format, legacy="Error: invalid limit", status="FAILED")

    try:
        from memory.brain import Brain
        from agent.embedder import embed
        from cognition.episodes import stitch_episodes
    except Exception as exc:
        err = error_payload("TIMELINE_BACKEND_UNAVAILABLE", f"{type(exc).__name__}", "life_timeline", None, "memory and cognition modules", True, "Verify memory and cognition packages are importable.")
        return _emit("life_timeline", started, started_at, trace_enabled, error=err, response_format=response_format, legacy="Error: timeline backend unavailable", status="FAILED")

    brain = Brain()
    episodes = stitch_episodes(list(brain.memories), embed_fn=embed)
    episodes = episodes[:limit]

    result = {"episodes": episodes, "count": len(episodes), "total_memories": len(brain.memories)}
    if not episodes:
        return _emit("life_timeline", started, started_at, trace_enabled, result=result, response_format=response_format, legacy="No episodes yet; not enough memories to form a timeline.", status="PARTIAL")

    lines = []
    for ep in episodes:
        span = ep.get("start_date", "")
        if ep.get("end_date") and ep["end_date"] != span:
            span = f"{span} to {ep['end_date']}"
        lines.append(f"- {span}: {ep.get('title', '')} ({ep.get('size', 0)} memories)")
    legacy = "Timeline:\n" + "\n".join(lines)
    return _emit("life_timeline", started, started_at, trace_enabled, result=result, response_format=response_format, legacy=legacy)
