import time

from memory.brain import Brain
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

_MEMORY_OPS_VERSION = "1.0.0"


def _brain() -> Brain:
    return Brain()


def _memory_trace(tool_name: str, started_at: str, started: float, inputs_received: int, path: str, status: str, fields: int, error_code: str | None = None) -> dict:
    return make_trace(
        tool_name,
        _MEMORY_OPS_VERSION,
        started_at,
        started,
        inputs_received,
        True,
        path,
        status,
        fields,
        {"count": 0, "systems": ["memory"]},
        error_code,
    )


def _memory_error(tool_name: str, error: dict, response_format: str, trace_enabled: bool, started: float, started_at: str, inputs_received: int, legacy: str):
    trace = _memory_trace(tool_name, started_at, started, inputs_received, "validate", "FAILED", 1, error["code"])
    emit_trace(trace, trace_enabled)
    if response_format == "structured":
        return structured_error(tool_name, _MEMORY_OPS_VERSION, error, started, trace)
    return legacy


def _memory_success(tool_name: str, result: dict, response_format: str, trace_enabled: bool, started: float, started_at: str, inputs_received: int, legacy: str, path: str, status: str = "SUCCESS"):
    trace = _memory_trace(tool_name, started_at, started, inputs_received, path, status, len(result))
    emit_trace(trace, trace_enabled)
    if response_format == "structured":
        return structured_success(tool_name, _MEMORY_OPS_VERSION, result, started, trace)
    return legacy


@tool(
    name="memory_assess",
    description="Assess memory index health, integrity checks, and GD tier readiness",
    examples=[
        "check memory system health",
        "memory tier report",
        "is memory index healthy",
    ],
    param_descriptions={
        "maintain": "When true, run backup and rebuild if integrity fails",
        "response_format": "legacy or structured",
        "trace_enabled": "Emit machine-readable trace when true",
    },
)
def memory_assess(maintain: bool = False, response_format: str = "legacy", trace_enabled: bool = False):
    started = time.perf_counter()
    started_at = utc_now_iso()
    inputs_received = 3
    response_format = normalize_response_format(response_format)
    trace_enabled = coerce_bool(trace_enabled)
    maintain = coerce_bool(maintain)
    brain = _brain()
    if maintain:
        report = brain.maintain(rebuild=False, backup=True)
        result = {"action": "maintain", **report}
        legacy = f"Memory maintain: status={report.get('status')} tier={report.get('after', {}).get('tier')}"
    else:
        assessment = brain.system_assessment()
        tier = brain.tier_report()
        result = {"action": "assess", "assessment": assessment, "tier": tier}
        legacy = f"Memory tier: {tier.get('tier')} entries={tier.get('entry_count')} coverage={tier.get('index_coverage_ratio')}"
    status = "SUCCESS" if result.get("after", {}).get("tier") == "gd" or result.get("tier", {}).get("tier") == "gd" else "PARTIAL"
    return _memory_success("memory_assess", result, response_format, trace_enabled, started, started_at, inputs_received, legacy, "assess", status)


@tool(
    name="memory_recall",
    description="Unified recall over text embeddings and the memory graph with automatic relation expansion",
    examples=[
        "recall what you know about my location",
        "memory search for python preference",
    ],
    param_descriptions={
        "query": "Recall query",
        "limit": "Maximum ranked memories to return, from 1 to 20",
        "include_context": "Include graph context string when true",
        "response_format": "legacy or structured",
        "trace_enabled": "Emit machine-readable trace when true",
    },
)
def memory_recall(
    query: str,
    limit: int = 5,
    include_context: bool = False,
    response_format: str = "legacy",
    trace_enabled: bool = False,
):
    started = time.perf_counter()
    started_at = utc_now_iso()
    inputs_received = 5
    response_format = normalize_response_format(response_format)
    trace_enabled = coerce_bool(trace_enabled)
    include_context = coerce_bool(include_context)
    query = str(query or "").strip()
    if not query:
        error = error_payload("EMPTY_QUERY", "query must not be empty.", "query", query, "non-empty recall query", False, "Pass text to search stored memories.")
        return _memory_error("memory_recall", error, response_format, trace_enabled, started, started_at, inputs_received, "Error: query is required")
    limit, limit_error = normalize_int(limit, "limit", 5, 1, 20, "Use limit between 1 and 20.", "INVALID_LIMIT")
    if limit_error is not None:
        return _memory_error("memory_recall", limit_error, response_format, trace_enabled, started, started_at, inputs_received, "Error: invalid limit")
    brain = _brain()
    unified = brain.recall_unified(query, k=limit)
    context = brain._unified_context_string(unified) if include_context else ""
    result = {
        "query": query,
        "unified": unified,
        "ranked": unified,
        "count": len(unified),
        "context": context,
        "storage": "unified",
    }
    legacy = context or " | ".join(item.get("text", "") for item in unified) or "No matching memories"
    status = "PARTIAL" if not unified else "SUCCESS"
    return _memory_success("memory_recall", result, response_format, trace_enabled, started, started_at, inputs_received, legacy, "recall", status)


@tool(
    name="memory_remember",
    description="Store a durable user fact in long-term memory",
    examples=[
        "remember that I prefer concise answers",
        "store that my name is Alex",
    ],
    param_descriptions={
        "text": "Fact to remember",
        "importance": "Importance from 0.0 to 1.0",
        "response_format": "legacy or structured",
        "trace_enabled": "Emit machine-readable trace when true",
    },
)
def memory_remember(text: str, importance: float = 0.8, response_format: str = "legacy", trace_enabled: bool = False):
    started = time.perf_counter()
    started_at = utc_now_iso()
    inputs_received = 4
    response_format = normalize_response_format(response_format)
    trace_enabled = coerce_bool(trace_enabled)
    text = str(text or "").strip()
    if not text:
        error = error_payload("EMPTY_TEXT", "text must not be empty.", "text", text, "non-empty memory fact", False, "Pass the fact to store.")
        return _memory_error("memory_remember", error, response_format, trace_enabled, started, started_at, inputs_received, "Error: text is required")
    brain = _brain()
    outcome = brain.remember(text, importance=importance)
    legacy = "Stored memory" if outcome.get("stored") else f"Memory not stored ({outcome.get('status')})"
    status = "SUCCESS" if outcome.get("stored") else "PARTIAL"
    return _memory_success("memory_remember", outcome, response_format, trace_enabled, started, started_at, inputs_received, legacy, "remember", status)


@tool(
    name="memory_forget",
    description="Remove memories matching a query string",
    examples=[
        "forget my old address",
        "remove memory about temporary password",
    ],
    param_descriptions={
        "query": "Text to match for removal",
        "response_format": "legacy or structured",
        "trace_enabled": "Emit machine-readable trace when true",
    },
)
def memory_forget(query: str, response_format: str = "legacy", trace_enabled: bool = False):
    started = time.perf_counter()
    started_at = utc_now_iso()
    inputs_received = 3
    response_format = normalize_response_format(response_format)
    trace_enabled = coerce_bool(trace_enabled)
    query = str(query or "").strip()
    if not query:
        error = error_payload("EMPTY_QUERY", "query must not be empty.", "query", query, "non-empty forget query", False, "Pass text to identify memories to remove.")
        return _memory_error("memory_forget", error, response_format, trace_enabled, started, started_at, inputs_received, "Error: query is required")
    brain = _brain()
    outcome = brain.forget(query)
    removed = outcome.get("removed") or []
    legacy = f"Removed {len(removed)} memories" if removed else f"No memories removed ({outcome.get('status')})"
    status = "SUCCESS" if removed else "PARTIAL"
    return _memory_success("memory_forget", outcome, response_format, trace_enabled, started, started_at, inputs_received, legacy, "forget", status)
