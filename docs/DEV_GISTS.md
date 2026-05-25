# Developer Gists from KING

These are the reusable engineering ideas inside KING, written as portable
notes for agent builders.

## 1. Markdown-Owned Tool Governance

Keep the runtime registry as the executable source of truth, then use markdown
files as the human-readable contract layer.

```text
tools/registry.py              callable schemas and dispatch
tools/TOOL_MANIFEST.md         active tool inventory and expected behavior
tools/TOOL_GROUNDING_POLICY.md action-claim rules
tools/TOOL_VERIFICATION_PIPELINE.md bounded ship checks
```

Why it works:

- developers can review behavior without reading every tool module
- tools stay discoverable without adding keyword-routing shortcuts
- verification expectations live beside the tool contracts

## 2. Graph Memory with Text Projections

Store every durable fact as graph memory, then keep a text projection for
embedding search, display, and rollback compatibility.

```text
fact text -> relation parser -> graph nodes and edges
          -> vector index text projection
          -> unified recall score
```

Useful implementation split:

- `memory/brain.py` owns ingest, recall, contradiction handling, and graph repair
- `memory/MEMORY_GRAPH_RELATIONS.md` owns relation rules
- `memory/MEMORY_AUTO_RELATIONS.md` owns automatic co-mention links
- `memory/MEMORY_UNIFIED_MODEL.md` documents the recall contract

## 3. Structured Tool Result Envelopes

Do not let the assistant invent success prose. Let tools return typed fields
and make the final answer compose from those fields.

```json
{
  "result": {
    "status": "ok",
    "items": [],
    "source": "provider",
    "source_status": "ok"
  },
  "meta": {
    "tool": "reddit",
    "version": "2.0.0",
    "duration_ms": 120
  },
  "trace": {
    "event": "TOOL TRACE",
    "status": "SUCCESS"
  }
}
```

This gives the assistant enough evidence to say what happened, what failed,
which provider responded, and whether fallback data was used.

## 4. JSON Tool-Call Leak Guard

Some models emit tool calls as text. KING handles both native tool calls and
JSON-shaped fallback text, then blocks raw action blobs from becoming user
visible output or persisted summaries.

Reusable rule:

```text
buffer model text -> inspect for tool-call shape -> execute if registered
                  -> return controlled error if unavailable
                  -> never store raw tool JSON as conversation memory
```

This keeps tool calling robust without changing the model.

## 5. Verification Pipeline as a Tool

Make verification an assistant capability instead of an informal checklist.

```text
markdown plan -> bounded commands -> per-check evidence -> ship or hold verdict
```

The useful part is not just running tests. It is forcing every claim to point
back to a visible command, exit code, timeout state, and stdout/stderr slice.

## 6. Frontend Tool Surfaces

Do not hide every capability in chat. Give complex tools a visual surface:

- memory graph for relationship recall and inspection
- navigator page for routes, places, distance, and path animation
- folder watcher dashboard for indexed files and service state
- gallery/viewer surfaces for generated assets

The chat stays conversational, while tool-specific pages make state inspectable.

## 7. Provider Honesty Pattern

External tools should report:

- provider called
- timeout and retry behavior
- fallback source, if any
- empty result versus provider failure
- partial success versus full success

That small distinction prevents the assistant from telling the user a tool
"found nothing" when the provider was actually blocked or unreachable.
