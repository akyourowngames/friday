"""Proactive Check: surface the best earned proactive thought, if any.

This connects the cognition proactive engine to the chat. It loads the persisted
proactive queue and cadence state (written by the cognition_scan maintenance
step), evaluates the current situational fit, and returns the single best
candidate that clears every gate, or nothing.

It honors the design: default is silence. When no candidate clears the adaptive
threshold, situational gate, and daily budget, it returns a quiet "nothing to
raise" result instead of inventing a message.
"""

import time
from datetime import datetime

from tools.registry import tool
from tools.runtime import (
    coerce_bool,
    emit_trace,
    error_payload,
    make_trace,
    normalize_response_format,
    structured_error,
    structured_success,
    utc_now_iso,
)

_PROACTIVE_VERSION = "1.0.0"


def _trace(name, started_at, started, schema_valid, status, output_fields, error_code=None):
    return make_trace(
        name, _PROACTIVE_VERSION, started_at, started, 1, schema_valid,
        "proactive_check", status, output_fields, {"count": 1, "systems": ["cognition_state"]}, error_code,
    )


def _emit(name, started, started_at, trace_enabled, result=None, error=None, response_format="legacy", legacy="", status="SUCCESS"):
    valid = error is None
    trace = _trace(name, started_at, started, valid, status if valid else "FAILED",
                   len(result) if result else 1, None if valid else error["code"])
    emit_trace(trace, trace_enabled)
    if response_format == "structured":
        if valid:
            return structured_success(name, _PROACTIVE_VERSION, result, started, trace)
        return structured_error(name, _PROACTIVE_VERSION, error, started, trace)
    return legacy


def _node_label(node_id: str) -> str:
    """Resolve a graph node id to its human name when possible."""
    try:
        from memory.brain import Brain

        graph = getattr(Brain(), "_graph", {})
        node = (graph.get("nodes") or {}).get(node_id) or {}
        return str(node.get("name") or node_id).replace("_", " ")
    except Exception:
        return str(node_id).replace("_", " ")


@tool(
    name="proactive_check",
    description="Check whether KING has an earned proactive observation to raise right now (from cadence deviations and the cognition queue). Returns nothing when it is better to stay quiet.",
    examples=[
        "anything on your mind",
        "is there anything I should know",
        "got any nudges for me",
    ],
    param_descriptions={
        "situational_fit": "0..1 estimate of how appropriate it is to speak now (default 0.7)",
        "response_format": "legacy or structured",
        "trace_enabled": "When true, emit a machine-readable trace entry",
    },
)
def proactive_check(situational_fit: float = 0.7, response_format: str = "legacy", trace_enabled: bool = False):
    started = time.perf_counter()
    started_at = utc_now_iso()
    response_format = normalize_response_format(response_format)
    trace_enabled = coerce_bool(trace_enabled)

    try:
        fit = float(situational_fit)
    except (TypeError, ValueError):
        fit = 0.7
    fit = min(1.0, max(0.0, fit))

    try:
        from cognition.state import load_state
        from cognition.proactive import ProactiveEngine
    except Exception as exc:
        err = error_payload("COGNITION_UNAVAILABLE", f"{type(exc).__name__}", "proactive_check", None, "cognition package", True, "Verify the cognition package is importable.")
        return _emit("proactive_check", started, started_at, trace_enabled, error=err, response_format=response_format, legacy="Error: cognition backend unavailable", status="FAILED")

    state = load_state()
    engine = ProactiveEngine.from_dict(state.get("proactive") or {})
    now = datetime.now()
    candidate = engine.select(situational_fit=fit, now=now)

    if candidate is None:
        result = {
            "has_message": False,
            "queue_size": engine.queue_size(),
            "budget_remaining": engine.budget_remaining(now),
            "threshold": round(engine.current_threshold(now), 3),
        }
        return _emit("proactive_check", started, started_at, trace_enabled, result=result, response_format=response_format, legacy="Nothing worth raising right now.", status="SUCCESS")

    # Structured signal for KING to phrase naturally. Never a canned sentence.
    node_label = _node_label(candidate.node) if candidate.node else ""
    signal = {
        "kind": candidate.source,
        "subject": node_label,
        "raw": candidate.content,
        "importance": round(candidate.importance, 3),
    }
    engine.mark_delivered(candidate, now=now)

    # Persist the delivery so the same thought is not raised again today.
    state["proactive"] = engine.to_dict()
    try:
        from cognition.state import save_state

        save_state(state)
    except Exception:
        pass

    result = {
        "has_message": True,
        "signal": signal,
        "budget_remaining": engine.budget_remaining(now),
    }
    legacy = f"Proactive signal: {signal['kind']} about {node_label or 'something'} (raise it naturally, with an easy exit)."
    return _emit("proactive_check", started, started_at, trace_enabled, result=result, response_format=response_format, legacy=legacy)
