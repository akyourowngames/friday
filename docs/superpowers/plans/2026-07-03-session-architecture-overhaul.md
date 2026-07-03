# Session Architecture Overhaul Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add session-scoped memory isolation, per-session JSONL conversations, session summaries, and a datetime tool to prevent stale memory pollution across sessions.

**Architecture:** Each session gets a UUID-based identity. Memory facts are tagged with session IDs and search defaults to current + recent N sessions. Conversations are dual-written to JSONL (per-session files) and SQLite (for backwards compat). Session summaries are auto-generated at exit and injected into the next session's context. A new `get_current_datetime` tool gives agents access to current time.

**Tech Stack:** Python 3.11+, SQLite, JSONL, Pydantic, croniter, Rich

---

## File Structure

### New Files
| File | Purpose |
|------|---------|
| `ares/session.py` | `SessionManager` — generates session IDs, tracks start time |
| `ares/sessions.py` | `SessionStore` — per-session JSONL file read/write |
| `ares/tools/datetime_tool.py` | `get_current_datetime` tool implementation |
| `tests/test_session.py` | Tests for SessionManager |
| `tests/test_sessions.py` | Tests for SessionStore |
| `tests/test_datetime_tool.py` | Tests for datetime tool |
| `tests/test_memory_session_scope.py` | Tests for scoped memory search |

### Modified Files
| File | Changes |
|------|---------|
| `ares/tools/definitions.py` | Add `get_current_datetime` tool schema |
| `ares/tools/executor.py` | Add `_get_current_datetime` handler + `_get_current_datetime` method |
| `ares/memory.py` | Add `session_id` column, scoped search with `scope` parameter |
| `ares/agent.py` | Accept `session_id`, forward to tool executor, inject previous session summary |
| `ares/cli.py` | Create `SessionManager` + `SessionStore`, dual-write, summary on exit |
| `ares/models.py` | Add `memory_session_scope` field to `AppConfig` |
| `ares/context_blend.py` | Add `previous_session_summary` parameter to `build_context_prompt` |

---

## Task 1: DateTime Tool

**Files:**
- Create: `ares/tools/datetime_tool.py`
- Create: `tests/test_datetime_tool.py`
- Modify: `ares/tools/definitions.py` (add tool schema)
- Modify: `ares/tools/executor.py` (add handler)

- [ ] **Step 1: Write the failing test for datetime tool logic**

Create `tests/test_datetime_tool.py`:

```python
"""Tests for the get_current_datetime tool."""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from ares.tools.datetime_tool import get_current_datetime_result


def test_returns_current_datetime():
    result = get_current_datetime_result()
    assert "datetime" in result
    assert "date" in result
    assert "time" in result
    assert "timezone" in result
    assert "day_of_week" in result
    assert "unix_timestamp" in result


def test_default_timezone_is_local():
    result = get_current_datetime_result()
    # Should be a valid timezone string
    assert isinstance(result["timezone"], str)
    assert len(result["timezone"]) > 0


def test_custom_timezone():
    result = get_current_datetime_result(timezone_name="America/New_York")
    assert result["timezone"] == "America/New_York"
    # Parse the datetime and verify it's in the right timezone
    dt = datetime.fromisoformat(result["datetime"])
    assert dt.tzinfo is not None


def test_date_format():
    result = get_current_datetime_result()
    # date should be YYYY-MM-DD
    assert len(result["date"]) == 10
    assert result["date"][4] == "-"
    assert result["date"][7] == "-"


def test_time_format():
    result = get_current_datetime_result()
    # time should be HH:MM:SS
    assert len(result["time"]) == 8
    assert result["time"][2] == ":"
    assert result["time"][5] == ":"


def test_day_of_week_is_valid():
    result = get_current_datetime_result()
    valid_days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    assert result["day_of_week"] in valid_days


def test_unix_timestamp_is_int():
    result = get_current_datetime_result()
    assert isinstance(result["unix_timestamp"], int)
    # Should be a reasonable timestamp (after 2020)
    assert result["unix_timestamp"] > 1577836800
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_datetime_tool.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'ares.tools.datetime_tool'`

- [ ] **Step 3: Implement the datetime tool**

Create `ares/tools/datetime_tool.py`:

```python
"""Get current datetime as a tool for the agent."""
from __future__ import annotations

import calendar
from datetime import datetime, timezone

from ares.tools.dates import local_timezone_name


def get_current_datetime_result(timezone_name: str | None = None) -> dict:
    """Return current date/time as a structured dict.

    Args:
        timezone_name: Optional IANA timezone (e.g. 'America/New_York').
                       Defaults to the system's local timezone.
    """
    tz_name = timezone_name or local_timezone_name()

    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = timezone.utc
        tz_name = "UTC"

    now = datetime.now(tz)

    return {
        "datetime": now.isoformat(timespec="seconds"),
        "date": now.strftime("%Y-%m-%d"),
        "time": now.strftime("%H:%M:%S"),
        "timezone": tz_name,
        "day_of_week": calendar.day_name[now.weekday()],
        "unix_timestamp": int(now.timestamp()),
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_datetime_tool.py -v`
Expected: All 7 tests PASS

- [ ] **Step 5: Add tool schema to definitions.py**

Append to the list in `get_tool_definitions()` in `ares/tools/definitions.py`:

```python
        _tool(
            "get_current_datetime",
            "Get the current date and time. Returns datetime, date, time, timezone, day of week, and unix timestamp.",
            {
                "timezone": {
                    "type": "string",
                    "description": "Optional IANA timezone (e.g. 'America/New_York'). Defaults to system timezone.",
                },
            },
        ),
```

- [ ] **Step 6: Add handler to executor.py**

In `ares/tools/executor.py`, add import at top:

```python
from ares.tools.datetime_tool import get_current_datetime_result as _get_current_datetime_impl
```

Add to `handlers` dict in `execute()`:

```python
            "get_current_datetime": self._get_current_datetime,
```

Add handler method to `ToolExecutor`:

```python
    def _get_current_datetime(self, args: dict) -> str:
        import json
        result = _get_current_datetime_impl(timezone_name=args.get("timezone"))
        return json.dumps(result, indent=2)
```

- [ ] **Step 7: Run all datetime tests**

Run: `pytest tests/test_datetime_tool.py -v`
Expected: All PASS

- [ ] **Step 8: Run full test suite to verify no regressions**

Run: `pytest tests/ -x --timeout=30`
Expected: All existing tests still pass

- [ ] **Step 9: Commit**

```bash
git add ares/tools/datetime_tool.py ares/tools/definitions.py ares/tools/executor.py tests/test_datetime_tool.py
git commit -m "feat: add get_current_datetime tool for agents and cron jobs"
```

---

## Task 2: Session Identity

**Files:**
- Create: `ares/session.py`
- Create: `tests/test_session.py`
- Modify: `ares/cli.py` (create SessionManager, pass session_id)

- [ ] **Step 1: Write the failing test**

Create `tests/test_session.py`:

```python
"""Tests for SessionManager."""
from ares.session import SessionManager


def test_session_id_format():
    sm = SessionManager()
    assert sm.session_id.startswith("sess-")
    assert len(sm.session_id) == 17  # "sess-" + 12 hex chars


def test_session_id_is_unique():
    sm1 = SessionManager()
    sm2 = SessionManager()
    assert sm1.session_id != sm2.session_id


def test_started_at_is_iso():
    sm = SessionManager()
    assert "T" in sm.started_at  # ISO format contains T


def test_get_id_returns_session_id():
    sm = SessionManager()
    assert sm.get_id() == sm.session_id
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_session.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'ares.session'`

- [ ] **Step 3: Implement SessionManager**

Create `ares/session.py`:

```python
"""Session identity management."""
from __future__ import annotations

from uuid import uuid4

from ares.tools.dates import now_local_iso


class SessionManager:
    """Generates and tracks a unique session ID."""

    def __init__(self) -> None:
        self.session_id: str = f"sess-{uuid4().hex[:12]}"
        self.started_at: str = now_local_iso()

    def get_id(self) -> str:
        return self.session_id
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_session.py -v`
Expected: All 4 tests PASS

- [ ] **Step 5: Commit**

```bash
git add ares/session.py tests/test_session.py
git commit -m "feat: add SessionManager for per-session identity"
```

---

## Task 3: Memory Isolation

**Files:**
- Create: `tests/test_memory_session_scope.py`
- Modify: `ares/memory.py` (add session_id column, scoped search)
- Modify: `ares/models.py` (add memory_session_scope to AppConfig)

- [ ] **Step 1: Write the failing test for session-scoped storage**

Create `tests/test_memory_session_scope.py`:

```python
"""Tests for session-scoped memory search."""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from ares.memory import MemoryStore


@pytest.fixture
def scoped_store(tmp_path):
    """Create a MemoryStore with a temporary database."""
    db_path = tmp_path / "test_memory.db"
    return MemoryStore(db_path=db_path)


def test_store_with_session_id(scoped_store):
    """Facts stored with a session_id should have it recorded."""
    fid = scoped_store.store("I like cats", session_id="sess-abc123")
    fact = scoped_store.get(fid)
    assert fact is not None
    assert fact["session_id"] == "sess-abc123"


def test_store_without_session_id_is_global(scoped_store):
    """Facts stored without session_id should be NULL (global)."""
    fid = scoped_store.store("Global fact")
    fact = scoped_store.get(fid)
    assert fact is not None
    assert fact["session_id"] is None


def test_search_scope_session_returns_current(scoped_store):
    """Session-scoped search should find facts from the current session."""
    scoped_store.store("Session fact", session_id="sess-current")
    scoped_store.store("Old fact", session_id="sess-old")

    results = scoped_store.search(
        "fact", scope="session", session_id="sess-current", recent_sessions=3
    )
    texts = [r["fact_text"] for r in results]
    assert "Session fact" in texts


def test_search_scope_session_includes_recent(scoped_store):
    """Session-scoped search should include recent sessions."""
    scoped_store.store("Current fact", session_id="sess-current")
    scoped_store.store("Recent fact", session_id="sess-recent")

    # Both should be found when searching with current session
    results = scoped_store.search(
        "fact", scope="session", session_id="sess-current", recent_sessions=3
    )
    texts = [r["fact_text"] for r in results]
    assert "Current fact" in texts
    assert "Recent fact" in texts


def test_search_scope_all_returns_everything(scoped_store):
    """Global search should return facts from all sessions."""
    scoped_store.store("Session fact", session_id="sess-current")
    scoped_store.store("Old fact", session_id="sess-old")

    results = scoped_store.search("fact", scope="all")
    texts = [r["fact_text"] for r in results]
    assert "Session fact" in texts
    assert "Old fact" in texts


def test_global_facts_always_searchable(scoped_store):
    """Facts without session_id should appear in both session and global search."""
    scoped_store.store("Global fact")
    scoped_store.store("Session fact", session_id="sess-current")

    results = scoped_store.search(
        "fact", scope="session", session_id="sess-current", recent_sessions=3
    )
    texts = [r["fact_text"] for r in results]
    assert "Global fact" in texts
    assert "Session fact" in texts


def test_session_id_migration(scoped_store):
    """Existing facts without session_id column should still work."""
    # Simulate pre-migration data by inserting directly
    scoped_store.conn.execute(
        "INSERT INTO facts_meta (fact_text, category) VALUES (?, ?)",
        ("Legacy fact", "note"),
    )
    scoped_store.conn.commit()

    results = scoped_store.search("Legacy", scope="all")
    assert len(results) >= 1
    assert results[0]["fact_text"] == "Legacy fact"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_memory_session_scope.py -v`
Expected: FAIL — `store()` doesn't accept `session_id`, `search()` doesn't accept `scope`/`session_id`/`recent_sessions`

- [ ] **Step 3: Add memory_session_scope to AppConfig**

In `ares/models.py`, add to `AppConfig`:

```python
    memory_session_scope: int = 3  # Search current + N recent sessions
```

- [ ] **Step 4: Add session_id column migration to MemoryStore**

In `ares/memory.py`, add to `_init_db()` after the existing `_ensure_column` calls:

```python
        _ensure_column(self.conn, "facts_meta", "session_id", "TEXT DEFAULT NULL")
```

- [ ] **Step 5: Update MemoryStore.store() to accept session_id**

In `ares/memory.py`, update the `store()` method signature and INSERT:

```python
    def store(
        self,
        fact_text: str,
        category: str = "note",
        confidence: float = 1.0,
        importance: float = 0.5,
        source: str = "conversation",
        session_id: str | None = None,
    ) -> int:
        """Store a new fact. Returns the fact_id."""
        cursor = self.conn.execute(
            """INSERT INTO facts_meta (fact_text, category, confidence, importance, source, session_id)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (fact_text, category, confidence, importance, source, session_id),
        )
```

- [ ] **Step 6: Update MemoryStore.search() to support scoped search**

In `ares/memory.py`, update the `search()` method:

```python
    def search(self, query: str, limit: int = 5, scope: str = "all",
               session_id: str | None = None, recent_sessions: int = 3) -> list[dict]:
        """Hybrid search: vector similarity + FTS5 keyword match, merged.

        Args:
            query: Search text.
            limit: Max results.
            scope: "session" to search current + recent N, "all" for everything.
            session_id: Current session ID (required when scope="session").
            recent_sessions: Number of recent sessions to include (default 3).
        """
        results = {}

        # Determine session filter SQL
        session_filter = ""
        session_params: list = []
        if scope == "session" and session_id:
            # Include: current session, recent N sessions, and global (NULL session_id)
            session_filter = """AND (session_id = ? OR session_id IS NULL
                OR session_id IN (
                    SELECT DISTINCT session_id FROM facts_meta
                    WHERE session_id IS NOT NULL
                    ORDER BY created_at DESC
                    LIMIT ?
                ))"""
            session_params = [session_id, recent_sessions]

        # 1. Vector search (semantic)
        if self.vector_enabled:
            try:
                query_vec = self._embed(query)
                vec_sql = f"""
                    SELECT rowid, distance FROM user_facts
                    WHERE embedding MATCH ? ORDER BY distance LIMIT ?
                """
                vec_rows = self.conn.execute(
                    vec_sql, (query_vec, limit * 2),
                ).fetchall()
                # Filter by session scope after vector search
                if session_filter:
                    valid_ids = set()
                    meta_rows = self.conn.execute(
                        f"SELECT fact_id FROM facts_meta WHERE fact_id IN ({','.join('?' for _ in vec_rows)}) {session_filter}",
                        [r["rowid"] for r in vec_rows] + session_params,
                    ).fetchall()
                    valid_ids = {r["fact_id"] for r in meta_rows}
                    vec_rows = [r for r in vec_rows if r["rowid"] in valid_ids]
                for row in vec_rows:
                    results[row["rowid"]] = {"distance": row["distance"], "source": "vector"}
            except Exception as exc:
                logger.debug("Vector memory search failed; falling back to FTS only: %s", exc)

        # 2. FTS5 keyword search
        try:
            fts_sql = f"""
                SELECT rowid, rank FROM facts_fts
                WHERE facts_fts MATCH ? ORDER BY rank LIMIT ?
            """
            fts_rows = self.conn.execute(
                fts_sql, (query, limit * 2),
            ).fetchall()
            # Filter by session scope after FTS search
            if session_filter:
                valid_ids = set()
                meta_rows = self.conn.execute(
                    f"SELECT fact_id FROM facts_meta WHERE fact_id IN ({','.join('?' for _ in fts_rows)}) {session_filter}",
                    [r["rowid"] for r in fts_rows] + session_params,
                ).fetchall()
                valid_ids = {r["fact_id"] for r in meta_rows}
                fts_rows = [r for r in fts_rows if r["rowid"] in valid_ids]
            for row in fts_rows:
                rid = row["rowid"]
                if rid in results:
                    results[rid]["fts_rank"] = row["rank"]
                    results[rid]["source"] = "both"
                else:
                    results[rid] = {"fts_rank": row["rank"], "source": "fts"}
        except Exception as exc:
            logger.debug("FTS memory search failed; using vector results only: %s", exc)

        if not results:
            return []

        # 3. Merge and fetch metadata
        row_ids = list(results.keys())
        placeholders = ",".join("?" * len(row_ids))
        meta_rows = self.conn.execute(
            f"SELECT * FROM facts_meta WHERE fact_id IN ({placeholders})",
            row_ids,
        ).fetchall()

        enriched = []
        for meta in meta_rows:
            entry = dict(meta)
            src = results[meta["fact_id"]]["source"]
            if src == "both":
                base_score = 0
            elif "distance" in results[meta["fact_id"]]:
                base_score = results[meta["fact_id"]]["distance"]
            else:
                base_score = 0.5
            entry["_score"] = self._rank_score(base_score, meta)
            enriched.append(entry)

        enriched.sort(key=lambda x: x["_score"])

        # 4. Update access stats
        for entry in enriched[:limit]:
            self.conn.execute(
                "UPDATE facts_meta SET last_accessed = datetime('now'), access_count = access_count + 1 WHERE fact_id = ?",
                (entry["fact_id"],),
            )
        self.conn.commit()

        return enriched[:limit]
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `pytest tests/test_memory_session_scope.py -v`
Expected: All 7 tests PASS

- [ ] **Step 8: Run full test suite**

Run: `pytest tests/ -x --timeout=30`
Expected: All existing tests still pass

- [ ] **Step 9: Commit**

```bash
git add ares/memory.py ares/models.py tests/test_memory_session_scope.py
git commit -m "feat: add session-scoped memory search to prevent stale cross-session facts"
```

---

## Task 4: Per-Session JSONL Conversations

**Files:**
- Create: `ares/sessions.py`
- Create: `tests/test_sessions.py`
- Modify: `ares/cli.py` (dual-write to JSONL + SQLite, read previous summary)

- [ ] **Step 1: Write the failing test**

Create `tests/test_sessions.py`:

```python
"""Tests for SessionStore (per-session JSONL conversations)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from ares.sessions import SessionStore


@pytest.fixture
def session_store(tmp_path):
    return SessionStore(data_dir=tmp_path)


def test_write_and_read_message(session_store):
    session_store.write_message("sess-abc", "user", "Hello!")
    entries = session_store.read_session("sess-abc")
    assert len(entries) == 1
    assert entries[0]["type"] == "message"
    assert entries[0]["role"] == "user"
    assert entries[0]["content"] == "Hello!"
    assert entries[0]["session_id"] == "sess-abc"


def test_write_multiple_messages(session_store):
    session_store.write_message("sess-abc", "user", "Hi")
    session_store.write_message("sess-abc", "assistant", "Hello!")
    session_store.write_message("sess-abc", "user", "How are you?")
    entries = session_store.read_session("sess-abc")
    assert len(entries) == 3
    assert entries[0]["role"] == "user"
    assert entries[1]["role"] == "assistant"
    assert entries[2]["role"] == "user"


def test_write_tool_calls(session_store):
    tool_calls = [{"name": "web_search", "arguments": {"query": "test"}}]
    session_store.write_message("sess-abc", "assistant", "Let me search...", tool_calls=tool_calls)
    entries = session_store.read_session("sess-abc")
    assert entries[0]["tool_calls"] == tool_calls


def test_write_summary(session_store):
    session_store.write_message("sess-abc", "user", "Hello")
    session_store.write_summary("sess-abc", "User said hello")
    entries = session_store.read_session("sess-abc")
    assert len(entries) == 2
    assert entries[1]["type"] == "summary"
    assert entries[1]["content"] == "User said hello"


def test_get_previous_summary(session_store):
    session_store.write_summary("sess-first", "First session summary")
    session_store.write_message("sess-second", "user", "Hi")
    summary = session_store.get_previous_summary("sess-second")
    assert summary == "First session summary"


def test_get_previous_summary_none_when_no_sessions(session_store):
    summary = session_store.get_previous_summary("sess-first")
    assert summary is None


def test_list_sessions(session_store):
    session_store.write_message("sess-aaa", "user", "Hello")
    session_store.write_message("sess-bbb", "user", "World")
    sessions = session_store.list_sessions()
    assert len(sessions) == 2
    ids = [s["session_id"] for s in sessions]
    assert "sess-aaa" in ids
    assert "sess-bbb" in ids


def test_read_nonexistent_session(session_store):
    entries = session_store.read_session("sess-nonexistent")
    assert entries == []


def test_jsonl_file_is_valid(session_store):
    """Each line in the JSONL file should be valid JSON."""
    session_store.write_message("sess-test", "user", "Hello")
    session_store.write_message("sess-test", "assistant", "Hi there!")
    jsonl_path = session_store.sessions_dir / "sess-test.jsonl"
    assert jsonl_path.exists()
    lines = jsonl_path.read_text().strip().split("\n")
    for line in lines:
        obj = json.loads(line)
        assert "type" in obj
        assert "timestamp" in obj
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_sessions.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'ares.sessions'`

- [ ] **Step 3: Implement SessionStore**

Create `ares/sessions.py`:

```python
"""Per-session JSONL conversation storage."""
from __future__ import annotations

import json
from pathlib import Path

from ares.tools.dates import now_local_iso


class SessionStore:
    """Manages per-session JSONL conversation files.

    Each session gets its own .jsonl file at {data_dir}/sessions/{session_id}.jsonl.
    Lines are append-only JSON objects with a 'type' field.
    """

    def __init__(self, data_dir: Path) -> None:
        self.sessions_dir = data_dir / "sessions"
        self.sessions_dir.mkdir(parents=True, exist_ok=True)

    def _session_path(self, session_id: str) -> Path:
        return self.sessions_dir / f"{session_id}.jsonl"

    def write_message(
        self,
        session_id: str,
        role: str,
        content: str,
        tool_calls: list[dict] | None = None,
        tool_call_id: str | None = None,
    ) -> None:
        """Append a message to the session's JSONL file."""
        entry = {
            "type": "message",
            "role": role,
            "content": content,
            "timestamp": now_local_iso(),
            "session_id": session_id,
        }
        if tool_calls:
            entry["tool_calls"] = tool_calls
        if tool_call_id:
            entry["tool_call_id"] = tool_call_id
        self._append(session_id, entry)

    def write_summary(self, session_id: str, summary: str) -> None:
        """Append a summary entry to the session's JSONL file."""
        entry = {
            "type": "summary",
            "content": summary,
            "timestamp": now_local_iso(),
            "session_id": session_id,
        }
        self._append(session_id, entry)

    def _append(self, session_id: str, entry: dict) -> None:
        path = self._session_path(session_id)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def read_session(self, session_id: str) -> list[dict]:
        """Read all entries from a session's JSONL file."""
        path = self._session_path(session_id)
        if not path.exists():
            return []
        entries = []
        for line in path.read_text(encoding="utf-8").strip().split("\n"):
            if line.strip():
                entries.append(json.loads(line))
        return entries

    def get_previous_summary(self, session_id: str) -> str | None:
        """Get the summary from the most recent session before this one.

        Lists all JSONL files, sorts by creation time, finds the session
        immediately before the given session_id, reads its last summary entry.
        Returns None if no previous session or no summary found.
        """
        sessions = self.list_sessions()
        # Find the session just before the current one
        current_idx = None
        for i, s in enumerate(sessions):
            if s["session_id"] == session_id:
                current_idx = i
                break

        if current_idx is None or current_idx >= len(sessions) - 1:
            return None

        # list_sessions returns newest first, so previous = current_idx + 1
        prev_session_id = sessions[current_idx + 1]["session_id"]
        entries = self.read_session(prev_session_id)
        # Find the last summary entry
        for entry in reversed(entries):
            if entry.get("type") == "summary":
                return entry["content"]
        return None

    def list_sessions(self) -> list[dict]:
        """List all sessions with metadata, newest first."""
        sessions = []
        for path in self.sessions_dir.glob("*.jsonl"):
            session_id = path.stem
            # Count messages (non-summary entries)
            message_count = 0
            first_timestamp = None
            for line in path.read_text(encoding="utf-8").strip().split("\n"):
                if not line.strip():
                    continue
                entry = json.loads(line)
                if entry.get("type") == "message":
                    message_count += 1
                if first_timestamp is None:
                    first_timestamp = entry.get("timestamp")
            sessions.append({
                "session_id": session_id,
                "started_at": first_timestamp,
                "message_count": message_count,
                "file_path": str(path),
            })
        # Sort by started_at descending (newest first)
        sessions.sort(key=lambda s: s.get("started_at") or "", reverse=True)
        return sessions
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_sessions.py -v`
Expected: All 9 tests PASS

- [ ] **Step 5: Integrate SessionStore into AresCLI**

In `ares/cli.py`, add import:

```python
from ares.session import SessionManager
from ares.sessions import SessionStore
```

In `AresCLI.__init__()`, after `self.conversation_store = ConversationStore()`, add:

```python
        self.session_manager = SessionManager()
        data_dir = Path(self.config.data_dir).expanduser()
        self.session_store = SessionStore(data_dir=data_dir)
```

In `AresCLI._process_input()`, after the existing `self.conversation_store.add_exchange(...)` line, add dual-write:

```python
        # Dual-write to JSONL session store
        self.session_store.write_message(
            self.session_manager.get_id(), "user", user_input
        )
        if full_response.strip():
            self.session_store.write_message(
                self.session_manager.get_id(), "assistant", full_response
            )
```

- [ ] **Step 6: Run tests**

Run: `pytest tests/test_sessions.py tests/test_session.py -v`
Expected: All PASS

- [ ] **Step 7: Commit**

```bash
git add ares/sessions.py ares/cli.py tests/test_sessions.py
git commit -m "feat: add per-session JSONL conversation storage with dual-write"
```

---

## Task 5: Session Summaries

**Files:**
- Modify: `ares/cli.py` (generate summary on exit, inject previous summary)
- Modify: `ares/agent.py` (accept session_id, read previous summary)
- Modify: `ares/context_blend.py` (add previous_session_summary parameter)

- [ ] **Step 1: Write the failing test for context injection**

Create or append to `tests/test_session.py`:

```python
def test_build_context_includes_previous_summary():
    from ares.context_blend import build_context_prompt
    result = build_context_prompt(
        previous_session_summary="User discussed Python testing",
        token_budget=2000,
    )
    assert "Previous session" in result
    assert "Python testing" in result


def test_build_context_omits_empty_summary():
    from ares.context_blend import build_context_prompt
    result = build_context_prompt(
        previous_session_summary="",
        token_budget=2000,
    )
    assert "Previous session" not in result


def test_build_context_omits_none_summary():
    from ares.context_blend import build_context_prompt
    result = build_context_prompt(
        previous_session_summary=None,
        token_budget=2000,
    )
    assert "Previous session" not in result
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_session.py -v -k "summary"`
Expected: FAIL — `build_context_prompt()` doesn't accept `previous_session_summary`

- [ ] **Step 3: Update build_context_prompt to accept previous_session_summary**

In `ares/context_blend.py`, update the function signature and body:

```python
def build_context_prompt(
    soul_context: str = "",
    profile_context: str = "",
    project_context: str = "",
    memories: list[dict] | None = None,
    conversation_summaries: list[str] | None = None,
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
        summary_section = f"## Previous Session Summary\n{previous_session_summary}"
        remaining = _append_section(sections, summary_section, remaining)

    summary_text = format_summaries(conversation_summaries)
    remaining = _append_section(sections, summary_text, remaining)

    if memories and remaining > 100:
        memory_section = format_memories(memories, token_budget=remaining)
        remaining = _append_section(sections, memory_section, remaining)

    return "\n\n".join(sections)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_session.py -v -k "summary"`
Expected: All 3 tests PASS

- [ ] **Step 5: Update Agent.get_context() to read previous session summary**

In `ares/agent.py`, update `get_context()` to accept and forward `session_id`:

```python
    def get_context(self, user_input: str, session_id: str | None = None) -> str:
        """Build full context: soul + profile + project + memories + previous session."""
        budgets = get_model_budgets(self.config.model)
        token_budget = budgets["context_token_budget"]
        max_retrieval = budgets["max_memory_retrieval"]

        soul_budget = max(200, token_budget // 10)
        profile_budget = max(400, token_budget // 5)
        project_budget = max(400, token_budget // 5)

        soul_ctx = self.soul_manager.get_context(token_budget=soul_budget)
        profile_ctx = self.profile_manager.get_context(token_budget=profile_budget)
        project_ctx = ""
        if self.config.project_context_enabled:
            project_ctx = self.project_context.get_context(token_budget=project_budget)

        # Session-scoped memory search
        search_scope = "session" if session_id else "all"
        memories = self.memory_store.search(
            user_input, limit=max_retrieval,
            scope=search_scope, session_id=session_id,
            recent_sessions=getattr(self.config, "memory_session_scope", 3),
        )

        summaries = []
        if self.conversation_store is not None:
            summaries = self.conversation_store.get_recent_summaries(limit=5)

        # Read previous session summary from JSONL
        prev_summary = None
        if session_id and hasattr(self, '_session_store') and self._session_store:
            prev_summary = self._session_store.get_previous_summary(session_id)

        return build_context_prompt(
            soul_context=soul_ctx,
            profile_context=profile_ctx,
            project_context=project_ctx,
            memories=memories,
            conversation_summaries=summaries,
            previous_session_summary=prev_summary,
            token_budget=token_budget,
        )
```

- [ ] **Step 6: Update Agent.__init__() to accept session_store**

In `ares/agent.py`, update `__init__` signature and body:

```python
    def __init__(
        self,
        memory_store: MemoryStore,
        conversation_store: ConversationStore | None = None,
        api_key: str = "",
        base_url: str = "",
        model: str = "",
        config: AppConfig | None = None,
        mcp_manager: Any | None = None,
        is_cron_session: bool = False,
        session_store: Any | None = None,
        session_id: str | None = None,
    ):
        # ... existing code ...
        self._session_store = session_store
        self._session_id = session_id
```

- [ ] **Step 7: Update Agent.run_stream() to pass session_id to get_context**

In `ares/agent.py`, update `run_stream()`:

```python
    async def run_stream(self, user_input: str, conversation_history: list[dict]) -> AsyncIterator[str]:
        context = self.get_context(user_input, session_id=self._session_id)
        messages = self.build_messages(user_input, conversation_history, context)
```

Also update `run()`:

```python
    async def run(self, user_input: str, conversation_history: list[dict]) -> AsyncIterator[str]:
        context = self.get_context(user_input, session_id=self._session_id)
        messages = self.build_messages(user_input, conversation_history, context)
```

- [ ] **Step 8: Update AresCLI to pass session_store and session_id to Agent**

In `ares/cli.py`, update Agent creation in `__init__()`:

```python
        self.agent = Agent(
            memory_store=self.memory_store,
            conversation_store=self.conversation_store,
            api_key=self.config.api_key,
            base_url=self.config.api_base_url,
            model=self.config.model,
            config=self.config,
            mcp_manager=self.mcp_manager,
            session_store=self.session_store,
            session_id=self.session_manager.get_id(),
        )
```

- [ ] **Step 9: Add summary generation on exit**

In `ares/cli.py`, in the `finally` block of `run()`, before closing the conversation store, add:

```python
            # Generate session summary and write to JSONL
            try:
                if self.conversation_history:
                    summary = self.conversation_store.summarize_conversation(
                        self.conversation_id
                    )
                    if summary:
                        self.session_store.write_summary(
                            self.session_manager.get_id(), summary
                        )
            except Exception as exc:
                self.console.print(f"[dim yellow]Shutdown warning (summary): {exc}[/dim yellow]")
```

- [ ] **Step 10: Run all tests**

Run: `pytest tests/test_session.py tests/test_sessions.py tests/test_memory_session_scope.py tests/test_datetime_tool.py -v`
Expected: All PASS

- [ ] **Step 11: Run full test suite**

Run: `pytest tests/ -x --timeout=30`
Expected: All existing tests still pass

- [ ] **Step 12: Commit**

```bash
git add ares/agent.py ares/cli.py ares/context_blend.py tests/test_session.py
git commit -m "feat: add session summaries with automatic generation and context injection"
```

---

## Verification Checklist

After all tasks are complete:

1. Run `pytest tests/ -x --timeout=30` — all tests pass
2. Start ares interactively: `python -m ares`
3. Have a conversation (e.g., "Remember that I prefer dark mode")
4. Exit with `/exit`
5. Verify `.ares/sessions/sess-*.jsonl` file exists with messages and summary
6. Restart ares: `python -m ares`
7. Verify "Previous Session Summary" appears in context (check with `/context`)
8. Ask "What do you remember about my preferences?" — should find "dark mode" from previous session
9. Test datetime tool: ask "What time is it?" — agent should use `get_current_datetime`
10. Verify memory isolation: store a fact in session 1, start session 2, search memory — session 1's fact should NOT appear by default (session-scoped search)
