"""Progressive tool disclosure for KING.

Implements the production pattern validated by GitHub's MCP server and described
in Anthropic's "Code Execution with MCP": instead of injecting every tool schema
into the prompt up front, the agent is given a small, always-available discovery
surface and loads only the specific tools it needs at runtime.

Two meta-tools:

- `find_tools(query)`  : semantic search over the full tool catalog AND the
  Composio capability index (every enabled gateway slug). Returns a short ranked
  list of candidates with how to call each. This is where the embedding ranking
  work now lives - as a search backend, not an up-front injection.
- `load_tool(names)`   : validate tool names and return their full signatures.
  The agent core detects this call and expands the active tool schema set for the
  rest of the turn, so the model can then call the loaded tool directly.

No regex, no keyword tables. Discovery is pure embedding similarity plus the
existing markdown-driven capability index. Local-first and config-driven.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import numpy as np

from config import settings
from tools.registry import tool, get_tool, get_tools
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

_DISCOVERY_VERSION = "1.0.0"

# Meta-tools are always available and must not be discoverable as candidates.
_META_TOOLS = {"find_tools", "load_tool"}

_CACHE_DIR = Path(settings.storage_dir)
_INDEX_EMB_CACHE = _CACHE_DIR / "discovery_embeddings.npy"
_INDEX_TEXTS_CACHE = _CACHE_DIR / "discovery_texts.json"


class _DiscoveryIndex:
    """Lazily built embedding index over all tools + composio capabilities."""

    def __init__(self):
        self._embeddings = None
        self._entries: list[dict] = []
        self._texts: list[str] = []

    def _build_entries(self) -> list[dict]:
        entries: list[dict] = []
        for t in get_tools():
            name = t["name"]
            if name in _META_TOOLS:
                continue
            text = f"{name}: {t['description']}"
            if t.get("examples"):
                text += " | " + " | ".join(t["examples"])
            entries.append(
                {
                    "kind": "tool",
                    "name": name,
                    "description": t["description"],
                    "text": text,
                    "call": {"name": name},
                }
            )
        # Composio capability index: each enabled slug becomes a discoverable
        # capability that resolves to the composio tool with the exact slug.
        try:
            from tools.capabilities import build_capability_rules

            for rule in build_capability_rules():
                backing = rule.get("tool")
                if not backing or get_tool(backing) is None:
                    continue
                args = rule.get("args") or {}
                slug = args.get("tool_slug", "")
                phrase = rule.get("phrase", "")
                if not phrase:
                    continue
                label = f"{backing}:{slug}" if slug else backing
                entries.append(
                    {
                        "kind": "capability",
                        "name": label,
                        "description": phrase,
                        "text": phrase,
                        "call": {"name": backing, "args": dict(args)},
                    }
                )
        except Exception:
            pass
        return entries

    def ensure(self):
        if self._embeddings is not None:
            return
        from agent.embedder import embed

        self._entries = self._build_entries()
        self._texts = [entry["text"] for entry in self._entries]
        if not self._texts:
            self._embeddings = np.zeros((0, 0), dtype=np.float32)
            return

        cached_texts = None
        if _INDEX_TEXTS_CACHE.exists():
            try:
                cached_texts = json.loads(_INDEX_TEXTS_CACHE.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                cached_texts = None

        if cached_texts == self._texts and _INDEX_EMB_CACHE.exists():
            try:
                self._embeddings = np.load(_INDEX_EMB_CACHE)
                return
            except (OSError, ValueError):
                pass

        self._embeddings = np.asarray(embed(self._texts), dtype=np.float32)
        if self._embeddings.ndim == 1:
            self._embeddings = self._embeddings.reshape(1, -1)
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        try:
            np.save(_INDEX_EMB_CACHE, self._embeddings)
            _INDEX_TEXTS_CACHE.write_text(json.dumps(self._texts, indent=2, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass

    def search(self, query: str, limit: int) -> list[dict]:
        self.ensure()
        if self._embeddings is None or self._embeddings.size == 0:
            return []
        from agent.embedder import embed

        q_emb = np.asarray(embed(query), dtype=np.float32).ravel()
        sims = np.dot(self._embeddings, q_emb)
        order = np.argsort(sims)[::-1]
        # Dedupe by the call target so the same tool/slug is not listed twice.
        results: list[dict] = []
        seen: set[str] = set()
        for idx in order:
            entry = self._entries[int(idx)]
            call = entry["call"]
            key = call.get("name", "") + "|" + str(call.get("args", {}).get("tool_slug", ""))
            if key in seen:
                continue
            seen.add(key)
            results.append(
                {
                    "name": entry["name"],
                    "backing_tool": call.get("name", ""),
                    "tool_slug": call.get("args", {}).get("tool_slug", ""),
                    "description": entry["description"],
                    "score": round(float(sims[int(idx)]), 4),
                }
            )
            if len(results) >= limit:
                break
        return results

    def invalidate(self):
        self._embeddings = None
        self._entries = []
        self._texts = []


_INDEX = _DiscoveryIndex()


def _trace(name, started_at, started, schema_valid, status, output_fields, error_code=None):
    return make_trace(
        name, _DISCOVERY_VERSION, started_at, started, 1, schema_valid,
        "discovery", status, output_fields, {"count": 0, "systems": []}, error_code,
    )


def _emit(name, started, started_at, trace_enabled, result=None, error=None, response_format="legacy", legacy="", status="SUCCESS"):
    valid = error is None
    trace = _trace(name, started_at, started, valid, status if valid else "FAILED",
                   len(result) if result else 1, None if valid else error["code"])
    emit_trace(trace, trace_enabled)
    if response_format == "structured":
        if valid:
            return structured_success(name, _DISCOVERY_VERSION, result, started, trace)
        return structured_error(name, _DISCOVERY_VERSION, error, started, trace)
    return legacy


@tool(
    name="find_tools",
    description=(
        "Search KING's full tool catalog for capabilities that match a need, when the tool "
        "you want is not already in the available list. Returns ranked candidate tools with how "
        "to call them. Follow up with load_tool to make a candidate callable."
    ),
    examples=[
        "find a tool to read my email",
        "search for a calendar tool",
        "what tool can post to slack",
        "find tools for google sheets",
    ],
    param_descriptions={
        "query": "Natural-language description of the capability you need",
        "limit": "Maximum candidates to return, from 1 to 15",
        "response_format": "legacy or structured",
        "trace_enabled": "When true, emit a machine-readable trace entry",
    },
)
def find_tools(query: str, limit: int = 8, response_format: str = "legacy", trace_enabled: bool = False):
    started = time.perf_counter()
    started_at = utc_now_iso()
    response_format = normalize_response_format(response_format)
    trace_enabled = coerce_bool(trace_enabled)
    query = str(query or "").strip()
    if not query:
        err = error_payload("EMPTY_QUERY", "query must not be empty.", "query", query, "capability description", False, "Describe the capability you need.")
        return _emit("find_tools", started, started_at, trace_enabled, error=err, response_format=response_format, legacy="Error: query is required", status="FAILED")
    limit, lim_err = normalize_int(limit, "limit", 8, 1, 15, "Use limit between 1 and 15.", "INVALID_LIMIT")
    if lim_err is not None:
        return _emit("find_tools", started, started_at, trace_enabled, error=lim_err, response_format=response_format, legacy="Error: invalid limit", status="FAILED")

    matches = _INDEX.search(query, limit)
    result = {"query": query, "matches": matches, "count": len(matches)}
    if not matches:
        return _emit("find_tools", started, started_at, trace_enabled, result=result, response_format=response_format, legacy="No matching tools found.", status="PARTIAL")
    lines = []
    for m in matches:
        if m["tool_slug"]:
            lines.append(f"- {m['backing_tool']} (tool_slug={m['tool_slug']}): {m['description']}")
        else:
            lines.append(f"- {m['name']}: {m['description']}")
    legacy = "Candidate tools:\n" + "\n".join(lines) + "\nUse load_tool with the tool name to make it callable."
    return _emit("find_tools", started, started_at, trace_enabled, result=result, response_format=response_format, legacy=legacy)


@tool(
    name="load_tool",
    description=(
        "Make one or more discovered tools callable for the rest of this turn. Pass the tool "
        "name(s) returned by find_tools. After loading, call the tool directly with its parameters."
    ),
    examples=[
        "load_tool composio",
        "load the weather tool",
        "load_tool web_search",
    ],
    param_descriptions={
        "names": "Comma-separated tool name(s) to load (registered tool names)",
        "response_format": "legacy or structured",
        "trace_enabled": "When true, emit a machine-readable trace entry",
    },
)
def load_tool(names: str, response_format: str = "legacy", trace_enabled: bool = False):
    started = time.perf_counter()
    started_at = utc_now_iso()
    response_format = normalize_response_format(response_format)
    trace_enabled = coerce_bool(trace_enabled)
    requested = [n.strip() for n in str(names or "").replace("|", ",").split(",") if n.strip()]
    if not requested:
        err = error_payload("EMPTY_NAMES", "names must not be empty.", "names", names, "tool name(s)", False, "Pass one or more tool names from find_tools.")
        return _emit("load_tool", started, started_at, trace_enabled, error=err, response_format=response_format, legacy="Error: names is required", status="FAILED")

    loaded = []
    unknown = []
    for name in requested:
        # Accept capability display names like "composio:GMAIL_FETCH_EMAILS" by
        # resolving to the backing registered tool before the slug separator.
        slug = ""
        if ":" in name:
            resolved, _, slug = name.partition(":")
            resolved = resolved.strip()
            slug = slug.strip()
        else:
            resolved = name
        info = get_tool(resolved)
        if info is None or resolved in _META_TOOLS:
            unknown.append(name)
            continue
        params = list(info.get("parameters", {}).get("properties", {}).keys())
        existing = next((t for t in loaded if t["name"] == resolved), None)
        if existing is None:
            entry = {"name": resolved, "parameters": params, "description": info.get("description", "")}
            if slug:
                entry["tool_slug"] = slug
            loaded.append(entry)
        elif slug and not existing.get("tool_slug"):
            existing["tool_slug"] = slug

    result = {"loaded": loaded, "unknown": unknown, "loaded_names": [t["name"] for t in loaded]}
    if not loaded:
        err = error_payload("UNKNOWN_TOOLS", "None of the requested tools are registered.", "names", names, "registered tool names", False, "Call find_tools first and use the exact names it returns.")
        return _emit("load_tool", started, started_at, trace_enabled, error=err, response_format=response_format, legacy=f"Error: unknown tool(s): {', '.join(unknown)}", status="FAILED")
    lines = [f"- {t['name']}({', '.join(t['parameters'])})" for t in loaded]
    legacy = "Loaded and now callable:\n" + "\n".join(lines)
    slug_notes = [f"{t['name']} with action=execute tool_slug={t['tool_slug']}" for t in loaded if t.get("tool_slug")]
    if slug_notes:
        legacy += "\nCall " + "; ".join(slug_notes) + "."
    if unknown:
        legacy += f"\nUnknown (ignored): {', '.join(unknown)}"
    return _emit("load_tool", started, started_at, trace_enabled, result=result, response_format=response_format, legacy=legacy)
