# Session Architecture Overhaul — Design Spec

> **Date:** 2026-07-03
> **Status:** Approved
> **Scope:** Session-scoped memory isolation, per-session JSONL conversations, session summaries, datetime tool

---

## Problem Statement

Ares currently has three architectural problems that degrade the user experience across sessions:

1. **Stale memory pollution**: `MemoryStore.search()` returns facts from ALL sessions indiscriminately. When a user discusses topic A in session 1, then topic B in session 2, session 2's context is polluted with irrelevant facts from session 1 — causing confusing, off-topic responses.

2. **No session boundary**: Conversation history is a flat SQLite table with no session-level separation. `get_recent_messages()` pulls from ALL conversations, mixing context across sessions.

3. **Missing datetime capability**: Cron jobs and agents have no tool to get the current date/time. Time calculations use inconsistent approaches (UTC vs local, `utc_now()` vs `datetime.now(timezone.utc)`).

---

## Design: Session-Scoped Memory Isolation

### Core Change

Add a `session_id` column to `facts_meta`. Each new session generates a UUID-based ID. Facts stored during a session are tagged with that session's ID.

### Search Behavior

- **Default (`scope="session"`)**: Search the current session + the N most recent sessions (configurable via `memory_session_scope`, default 3).
- **Global (`scope="all"`)**: Search ALL sessions. Used by `/memory` command.
- **Backwards compatible**: Facts without a `session_id` (pre-migration) are treated as "global" — always searchable in both modes.

### Migration

Use the existing `_ensure_column` pattern:
```python
_ensure_column(self.conn, "facts_meta", "session_id", "TEXT DEFAULT NULL")
```

No backfill needed. Old facts remain global (NULL session_id = always searchable).

### API Changes

```python
# memory.py
def store(self, fact_text, category="note", confidence=1.0, importance=0.5,
          source="conversation", session_id: str | None = None) -> int:
    ...

def search(self, query: str, limit: int = 5, scope: str = "session",
           session_id: str | None = None, recent_sessions: int = 3) -> list[dict]:
    ...
```

### Files Modified
- `ares/memory.py` — Add `session_id` column, scoped search
- `ares/config.py` — Add `memory_session_scope` field (default 3)

---

## Design: Session Identity

### Core Change

New `SessionManager` class generates a unique session ID at startup and provides it to all components.

### Session ID Format

`sess-{uuid4().hex[:12]}` — short, readable, collision-resistant.

### SessionManager API

```python
# ares/session.py
class SessionManager:
    def __init__(self):
        self.session_id: str = f"sess-{uuid4().hex[:12]}"
        self.started_at: str = now_local_iso()

    def get_id(self) -> str:
        return self.session_id
```

### Integration

- `AresCLI.__init__()`: Create `SessionManager`, pass `session_id` to `MemoryStore.store()` and `SessionStore`
- `agent.py`: Accept `session_id`, pass through to tool executor for memory writes

### Files Created
- `ares/session.py` — SessionManager class

### Files Modified
- `ares/cli.py` — Create SessionManager, pass session_id to components
- `ares/agent.py` — Accept and forward session_id
- `ares/tools/__init__.py` — Pass session_id to MemoryStore.store() in memory tools

---

## Design: Per-Session JSONL Conversations

### Core Change

Each session creates its own JSONL file at `.ares/sessions/{session_id}.jsonl`. SQLite conversations are kept for backwards compatibility and search.

### JSONL Schema

Each line is a self-contained JSON object:

```jsonl
{"type": "message", "role": "user", "content": "What's the weather?", "timestamp": "2026-07-03T14:30:00-04:00", "session_id": "sess-abc123def456"}
{"type": "message", "role": "assistant", "content": "Let me check...", "timestamp": "...", "session_id": "...", "tool_calls": [{"name": "web_search", "arguments": {...}}]}
{"type": "message", "role": "tool", "tool_call_id": "call_0", "content": "Sunny, 75°F", "timestamp": "...", "session_id": "..."}
{"type": "message", "role": "assistant", "content": "It's sunny and 75°F.", "timestamp": "...", "session_id": "..."}
{"type": "summary", "content": "User asked about weather. Checked web search. Reported sunny 75°F.", "timestamp": "...", "session_id": "..."}
```

### SessionStore API

```python
# ares/sessions.py
class SessionStore:
    def __init__(self, data_dir: Path):
        self.sessions_dir = data_dir / "sessions"
        self.sessions_dir.mkdir(parents=True, exist_ok=True)

    def write_message(self, session_id: str, role: str, content: str,
                      tool_calls: list | None = None, tool_call_id: str | None = None) -> None:
        """Append a message to the session's JSONL file."""
        ...

    def write_summary(self, session_id: str, summary: str) -> None:
        """Append a summary entry to the session's JSONL file."""
        ...

    def read_session(self, session_id: str) -> list[dict]:
        """Read all entries from a session's JSONL file."""
        ...

    def get_previous_summary(self, session_id: str) -> str | None:
        """Get the summary from the most recent session before this one.
        
        Lists all JSONL files, sorts by creation time, finds the session
        immediately before the given session_id, reads its last summary entry.
        Returns None if no previous session or no summary found.
        """
        ...

    def list_sessions(self) -> list[dict]:
        """List all sessions with metadata (id, start time, message count)."""
        ...
```

### Integration

- `cli.py`: Create `SessionStore` alongside `ConversationStore`. Write messages to both JSONL and SQLite (dual-write during transition).
- `agent.py`: Read previous session summary for context.
- `cli.py` `finally` block: Generate summary, write to JSONL.

### Files Created
- `ares/sessions.py` — SessionStore class

### Files Modified
- `ares/cli.py` — Dual-write to JSONL + SQLite, read previous summary
- `ares/agent.py` — Include previous session summary in context

---

## Design: Session Summaries

### Core Change

When a session ends (`/exit` or Ctrl+C), auto-generate a compact summary and append it as a `summary` entry in the JSONL file.

### Summary Generation

Local, no LLM call. Reuse the existing `ConversationStore.summarize_conversation()` pattern:
1. Extract user topics (first 5 user messages)
2. Extract assistant responses (first 3 assistant messages)
3. Combine into compact string (max 1200 chars)

### Context Injection

The next session reads the most recent summary and includes it in the system prompt:
```
## Previous Session Summary
{summary}
```

### Integration

- `cli.py` `finally` block: Call `session_store.write_summary(session_id, summary)` after generating summary
- `agent.py` `get_context()`: Call `session_store.get_previous_summary(session_id)` and include in context

### Files Modified
- `ares/cli.py` — Write summary on exit
- `ares/agent.py` — Read previous summary for context

---

## Design: DateTime Tool

### Core Change

Add a `get_current_datetime` tool that returns the current date/time in the user's configured timezone.

### Tool Definition

```python
{
    "type": "function",
    "function": {
        "name": "get_current_datetime",
        "description": "Get the current date and time in the configured timezone.",
        "parameters": {
            "type": "object",
            "properties": {
                "timezone": {
                    "type": "string",
                    "description": "Optional timezone override (e.g. 'America/New_York'). Defaults to system timezone."
                }
            },
            "required": []
        }
    }
}
```

### Response Format

```json
{
    "datetime": "2026-07-03T14:30:00",
    "date": "2026-07-03",
    "time": "14:30:00",
    "timezone": "America/New_York",
    "day_of_week": "Thursday",
    "unix_timestamp": 1751556600
}
```

### Files Created
- `ares/tools/datetime_tool.py` — Tool definition + executor

### Files Modified
- `ares/tools/__init__.py` — Register datetime tool

---

## Implementation Order

1. **DateTime tool** — Simplest, standalone, immediate value for cron jobs
2. **Session identity** — Foundation for memory isolation
3. **Memory isolation** — Add session_id, scoped search
4. **Per-session JSONL** — SessionStore, dual-write
5. **Session summaries** — Auto-summarize on exit, inject into next session

---

## Testing Strategy

- **Unit tests** for each new module (`test_session.py`, `test_sessions.py`, `test_datetime_tool.py`)
- **Integration tests** for memory scoping (store with session_id, search with scope)
- **Regression tests** ensure existing tests still pass (427+ tests)
- **Manual test**: Run ares, have a conversation, exit, restart, verify previous session summary appears in context

---

## Migration Safety

- All changes are additive (new columns, new files, new tools)
- SQLite `_ensure_column` pattern for schema migration
- Dual-write (JSONL + SQLite) during transition
- Old facts without session_id remain globally searchable
- No breaking changes to existing APIs
