# Task Execution Engine — Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Transform the Ares task system from simple pending/done states into a full execution engine with LLM-generated plans, step-by-step tracking, resume, retry, execution logs, artifact tracking, and completion reports.

**Architecture:** Extend the existing SQLite-based TaskStore with new columns (plan, state, current_step, etc.) and two new tables (task_events, task_artifacts). Rewrite TaskExecutor to run a planning → step-by-step execution → completion report pipeline. Each step runs a focused mini agent loop. Failed tasks resume from the last uncompleted step.

**Tech Stack:** Python 3.11+, SQLite (sqlite3), asyncio, existing Ares LLM client and ToolExecutor.

**Spec:** `docs/superpowers/specs/2026-06-23-task-execution-engine-design.md`

---

## File Map

| File | Action | Responsibility |
|------|--------|----------------|
| `ares/tools/tasks.py` | Modify | DB migration, new query methods |
| `ares/planner.py` | Create | LLM-based task planner |
| `ares/models.py` | Modify | TaskState enum, transitions |
| `ares/task_executor.py` | Rewrite | Core execution engine |
| `ares/tools/definitions.py` | Modify | 3 new tool definitions |
| `ares/tools/executor.py` | Modify | 3 new tool handlers |
| `ares/server.py` | Modify | Wire executor, WebSocket events |
| `ares/cli.py` | Modify | New task commands |
| `tests/test_tasks.py` | Modify | Migration + new method tests |
| `tests/test_planner.py` | Create | Planner unit tests |
| `tests/test_task_executor.py` | Rewrite | Executor integration tests |

---

## Task 1: Database Schema Migration

**Files:**
- Modify: `ares/tools/tasks.py:27-53` (end of `_init_db`)
- Test: `tests/test_tasks.py`

- [ ] **Step 1: Write failing test for migration**

Open `tests/test_tasks.py` and add at the end:

```python
class TestMigrateV2:
    """Tests for the v2 schema migration."""

    def _make_store(self, tmp_path):
        from ares.tools.tasks import TaskStore
        return TaskStore(db_path=tmp_path / "test.db")

    def test_migrate_adds_state_column(self, tmp_path):
        store = self._make_store(tmp_path)
        # Verify state column exists
        rows = store.conn.execute("PRAGMA table_info(tasks)").fetchall()
        columns = {row["name"] for row in rows}
        assert "state" in columns

    def test_migrate_adds_plan_column(self, tmp_path):
        store = self._make_store(tmp_path)
        rows = store.conn.execute("PRAGMA table_info(tasks)").fetchall()
        columns = {row["name"] for row in rows}
        assert "plan" in columns

    def test_migrate_adds_step_columns(self, tmp_path):
        store = self._make_store(tmp_path)
        rows = store.conn.execute("PRAGMA table_info(tasks)").fetchall()
        columns = {row["name"] for row in rows}
        assert "current_step" in columns
        assert "total_steps" in columns
        assert "completed_steps" in columns

    def test_migrate_adds_retry_columns(self, tmp_path):
        store = self._make_store(tmp_path)
        rows = store.conn.execute("PRAGMA table_info(tasks)").fetchall()
        columns = {row["name"] for row in rows}
        assert "attempt" in columns
        assert "max_attempts" in columns
        assert "retry_reason" in columns

    def test_migrate_adds_completion_report(self, tmp_path):
        store = self._make_store(tmp_path)
        rows = store.conn.execute("PRAGMA table_info(tasks)").fetchall()
        columns = {row["name"] for row in rows}
        assert "completion_report" in columns

    def test_migrate_creates_task_events_table(self, tmp_path):
        store = self._make_store(tmp_path)
        tables = {row[0] for row in store.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        assert "task_events" in tables

    def test_migrate_creates_task_artifacts_table(self, tmp_path):
        store = self._make_store(tmp_path)
        tables = {row[0] for row in store.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        assert "task_artifacts" in tables

    def test_migrate_maps_done_to_completed(self, tmp_path):
        store = self._make_store(tmp_path)
        # Insert a task with old 'done' status
        store.conn.execute(
            "INSERT INTO tasks (title, status) VALUES (?, ?)",
            ("old task", "done"),
        )
        store.conn.commit()
        store._migrate_v2()
        row = store.conn.execute("SELECT state FROM tasks WHERE title = 'old task'").fetchone()
        assert row["state"] == "completed"

    def test_migrate_maps_partial_to_failed(self, tmp_path):
        store = self._make_store(tmp_path)
        store.conn.execute(
            "INSERT INTO tasks (title, status) VALUES (?, ?)",
            ("partial task", "partial"),
        )
        store.conn.commit()
        store._migrate_v2()
        row = store.conn.execute("SELECT state FROM tasks WHERE title = 'partial task'").fetchone()
        assert row["state"] == "failed"

    def test_migrate_is_idempotent(self, tmp_path):
        store = self._make_store(tmp_path)
        store._migrate_v2()
        store._migrate_v2()  # Should not raise
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd C:\Users\anime\ares && python -m pytest tests/test_tasks.py::TestMigrateV2 -x -v`
Expected: FAIL with `AttributeError` (no `_migrate_v2` method yet)

- [ ] **Step 3: Implement the migration**

Open `ares/tools/tasks.py`. Add the migration method to `TaskStore` and call it from `_init_db`:

After line 53 (`self.conn.commit()`), add:

```python
        self._migrate_v2()
```

Then add the method itself after `_init_db`:

```python
    def _migrate_v2(self):
        """Add v2 columns for task execution engine (state, plan, steps, retry, events, artifacts)."""
        # New columns on tasks table
        _ensure_column(self.conn, "tasks", "state", "TEXT DEFAULT 'pending'")
        _ensure_column(self.conn, "tasks", "plan", "TEXT")
        _ensure_column(self.conn, "tasks", "current_step", "INTEGER DEFAULT 0")
        _ensure_column(self.conn, "tasks", "total_steps", "INTEGER DEFAULT 0")
        _ensure_column(self.conn, "tasks", "completed_steps", "TEXT")
        _ensure_column(self.conn, "tasks", "attempt", "INTEGER DEFAULT 1")
        _ensure_column(self.conn, "tasks", "max_attempts", "INTEGER DEFAULT 3")
        _ensure_column(self.conn, "tasks", "retry_reason", "TEXT")
        _ensure_column(self.conn, "tasks", "completion_report", "TEXT")

        # Migrate existing status values to new state column
        self.conn.execute("UPDATE tasks SET state = 'completed' WHERE status = 'done' AND (state IS NULL OR state = 'pending')")
        self.conn.execute("UPDATE tasks SET state = 'failed' WHERE status = 'partial' AND (state IS NULL OR state = 'pending')")
        self.conn.execute("UPDATE tasks SET state = 'cancelled' WHERE status = 'cancelled' AND (state IS NULL OR state = 'pending')")
        self.conn.execute("UPDATE tasks SET state = 'running' WHERE status = 'in_progress' AND (state IS NULL OR state = 'pending')")

        # New tables
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS task_events (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id    INTEGER NOT NULL,
                timestamp  TEXT DEFAULT (datetime('now')),
                level      TEXT DEFAULT 'info',
                step       INTEGER,
                message    TEXT NOT NULL,
                FOREIGN KEY (task_id) REFERENCES tasks(id)
            )
        """)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS task_artifacts (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id       INTEGER NOT NULL,
                step          INTEGER,
                path          TEXT NOT NULL,
                artifact_type TEXT NOT NULL,
                size_bytes    INTEGER DEFAULT 0,
                size_human    TEXT DEFAULT '0 B',
                line_count    INTEGER,
                description   TEXT,
                created_at    TEXT DEFAULT (datetime('now')),
                FOREIGN KEY (task_id) REFERENCES tasks(id)
            )
        """)
        self.conn.commit()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd C:\Users\anime\ares && python -m pytest tests/test_tasks.py::TestMigrateV2 -x -v`
Expected: All 11 tests PASS

- [ ] **Step 5: Commit**

```bash
git add ares/tools/tasks.py tests/test_tasks.py
git commit -m "feat(tasks): add v2 schema migration with state, plan, events, artifacts tables"
```

---

## Task 2: TaskStore New Query Methods

**Files:**
- Modify: `ares/tools/tasks.py` (add methods after `cancel`)
- Test: `tests/test_tasks.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/test_tasks.py`:

```python
class TestTaskStoreV2Methods:
    """Tests for the new v2 TaskStore methods."""

    def _make_store(self, tmp_path):
        from ares.tools.tasks import TaskStore
        return TaskStore(db_path=tmp_path / "test.db")

    def _make_task(self, store, title="test task"):
        task_id = store.create(title)
        return task_id

    def test_add_event_returns_id(self, tmp_path):
        store = self._make_store(tmp_path)
        task_id = self._make_task(store)
        event_id = store.add_event(task_id, level="info", step=None, message="test event")
        assert isinstance(event_id, int)
        assert event_id > 0

    def test_get_events_returns_ordered(self, tmp_path):
        store = self._make_store(tmp_path)
        task_id = self._make_task(store)
        store.add_event(task_id, level="info", step=None, message="first")
        store.add_event(task_id, level="success", step=1, message="second")
        store.add_event(task_id, level="error", step=2, message="third")
        events = store.get_events(task_id)
        assert len(events) == 3
        assert events[0]["message"] == "first"
        assert events[2]["message"] == "third"

    def test_get_events_filters_by_task(self, tmp_path):
        store = self._make_store(tmp_path)
        id1 = self._make_task(store, "task 1")
        id2 = self._make_task(store, "task 2")
        store.add_event(id1, level="info", step=None, message="t1 event")
        store.add_event(id2, level="info", step=None, message="t2 event")
        events = store.get_events(id1)
        assert len(events) == 1
        assert events[0]["message"] == "t1 event"

    def test_add_artifact_returns_id(self, tmp_path):
        store = self._make_store(tmp_path)
        task_id = self._make_task(store)
        artifact_id = store.add_artifact(task_id, {
            "step": 1,
            "path": "/tmp/test.md",
            "artifact_type": "write_file",
            "size_bytes": 1024,
            "size_human": "1.0 KB",
            "line_count": 50,
            "description": "Test file",
        })
        assert isinstance(artifact_id, int)
        assert artifact_id > 0

    def test_get_artifacts_returns_all(self, tmp_path):
        store = self._make_store(tmp_path)
        task_id = self._make_task(store)
        store.add_artifact(task_id, {"step": 1, "path": "a.md", "artifact_type": "write_file", "size_bytes": 10, "size_human": "10 B", "line_count": None, "description": None})
        store.add_artifact(task_id, {"step": 2, "path": "b.py", "artifact_type": "edit_file", "size_bytes": 20, "size_human": "20 B", "line_count": None, "description": None})
        artifacts = store.get_artifacts(task_id)
        assert len(artifacts) == 2
        assert artifacts[0]["path"] == "a.md"
        assert artifacts[1]["path"] == "b.py"

    def test_get_tasks_by_state_filters(self, tmp_path):
        store = self._make_store(tmp_path)
        id1 = store.create("running task")
        id2 = store.create("done task")
        store.update(id1, state="running")
        store.update(id2, state="completed")
        running = store.get_tasks_by_state("running")
        assert len(running) == 1
        assert running[0]["id"] == id1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd C:\Users\anime\ares && python -m pytest tests/test_tasks.py::TestTaskStoreV2Methods -x -v`
Expected: FAIL with `AttributeError`

- [ ] **Step 3: Implement the methods**

Add these methods to `TaskStore` in `ares/tools/tasks.py` (after the `cancel` method):

```python
    # ── v2: Events ─────────────────────────────────────────────

    def add_event(self, task_id: int, level: str, step: int | None, message: str) -> int:
        """Insert a task event. Returns event ID."""
        cursor = self.conn.execute(
            "INSERT INTO task_events (task_id, level, step, message) VALUES (?, ?, ?, ?)",
            (task_id, level, step, message),
        )
        self.conn.commit()
        return cursor.lastrowid

    def get_events(self, task_id: int, limit: int = 50) -> list[dict]:
        """Get events for a task, oldest first."""
        rows = self.conn.execute(
            "SELECT * FROM task_events WHERE task_id = ? ORDER BY id ASC LIMIT ?",
            (task_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    # ── v2: Artifacts ──────────────────────────────────────────

    def add_artifact(self, task_id: int, artifact: dict) -> int:
        """Insert a task artifact. Returns artifact ID."""
        cursor = self.conn.execute(
            """INSERT INTO task_artifacts
               (task_id, step, path, artifact_type, size_bytes, size_human, line_count, description)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                task_id,
                artifact.get("step"),
                artifact.get("path", ""),
                artifact.get("artifact_type", "unknown"),
                artifact.get("size_bytes", 0),
                artifact.get("size_human", "0 B"),
                artifact.get("line_count"),
                artifact.get("description"),
            ),
        )
        self.conn.commit()
        return cursor.lastrowid

    def get_artifacts(self, task_id: int) -> list[dict]:
        """Get all artifacts for a task, oldest first."""
        rows = self.conn.execute(
            "SELECT * FROM task_artifacts WHERE task_id = ? ORDER BY id ASC",
            (task_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    # ── v2: State management ───────────────────────────────────

    def set_state(self, task_id: int, state: str) -> bool:
        """Update task state and updated_at timestamp."""
        now = now_local_iso()
        cursor = self.conn.execute(
            "UPDATE tasks SET state = ?, updated_at = ? WHERE id = ?",
            (state, now, task_id),
        )
        self.conn.commit()
        return cursor.rowcount > 0

    def get_tasks_by_state(self, state: str, limit: int = 50) -> list[dict]:
        """Get tasks filtered by state."""
        rows = self.conn.execute(
            "SELECT * FROM tasks WHERE state = ? ORDER BY created_at DESC LIMIT ?",
            (state, limit),
        ).fetchall()
        return [dict(r) for r in rows]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd C:\Users\anime\ares && python -m pytest tests/test_tasks.py::TestTaskStoreV2Methods -x -v`
Expected: All 7 tests PASS

- [ ] **Step 5: Commit**

```bash
git add ares/tools/tasks.py tests/test_tasks.py
git commit -m "feat(tasks): add event/artifact/state query methods to TaskStore"
```

---

## Task 3: TaskPlanner Module

**Files:**
- Create: `ares/planner.py`
- Create: `tests/test_planner.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_planner.py`:

```python
"""Tests for the task planner module."""

import json
import pytest
from unittest.mock import AsyncMock, MagicMock
from ares.planner import TaskPlanner


def _make_planner(response_text):
    """Create a TaskPlanner with a mocked LLM that returns the given text."""
    mock_llm = AsyncMock()
    mock_llm.chat = AsyncMock(return_value={"content": response_text})
    return TaskPlanner(mock_llm)


class TestParsePlan:
    def test_parse_json_array(self):
        planner = _make_planner("[]")
        result = planner._parse_plan('[{"step": 1, "title": "Do thing", "description": "desc"}]')
        assert len(result) == 1
        assert result[0]["title"] == "Do thing"
        assert result[0]["status"] == "pending"

    def test_parse_markdown_code_block(self):
        planner = _make_planner("")
        text = 'Here is the plan:\n```json\n[{"step": 1, "title": "Step one", "description": "desc"}]\n```'
        result = planner._parse_plan(text)
        assert len(result) == 1
        assert result[0]["title"] == "Step one"

    def test_parse_inline_json(self):
        planner = _make_planner("")
        text = 'Plan: [{"step": 1, "title": "Search", "description": "find stuff"}]'
        result = planner._parse_plan(text)
        assert len(result) == 1

    def test_parse_invalid_raises(self):
        planner = _make_planner("")
        with pytest.raises(ValueError, match="Could not parse"):
            planner._parse_plan("no json here at all")

    def test_validate_renumbers_steps(self):
        planner = _make_planner("")
        result = planner._validate_plan([
            {"step": 5, "title": "A", "description": ""},
            {"step": 99, "title": "B", "description": ""},
        ])
        assert result[0]["step"] == 1
        assert result[1]["step"] == 2

    def test_validate_truncates_long_titles(self):
        planner = _make_planner("")
        result = planner._validate_plan([{"step": 1, "title": "x" * 100, "description": ""}])
        assert len(result[0]["title"]) == 60

    def test_validate_empty_list_raises(self):
        planner = _make_planner("")
        with pytest.raises(ValueError, match="empty"):
            planner._validate_plan([])


class TestFallbackPlan:
    def test_fallback_has_one_step(self):
        planner = _make_planner("")
        result = planner._fallback_plan({"title": "My task", "description": "desc"})
        assert len(result) == 1
        assert result[0]["title"] == "My task"
        assert result[0]["status"] == "pending"

    def test_fallback_uses_title_as_description(self):
        planner = _make_planner("")
        result = planner._fallback_plan({"title": "Do stuff"})
        assert result[0]["description"] == "Do stuff"


class TestGeneratePlan:
    @pytest.mark.asyncio
    async def test_generate_plan_returns_list(self):
        plan_data = [
            {"step": 1, "title": "Step one", "description": "desc one"},
            {"step": 2, "title": "Step two", "description": "desc two"},
        ]
        planner = _make_planner(json.dumps(plan_data))
        result = await planner.generate_plan({"title": "test", "description": "desc"})
        assert len(result) == 2
        assert result[0]["status"] == "pending"

    @pytest.mark.asyncio
    async def test_generate_plan_fallback_on_error(self):
        mock_llm = AsyncMock()
        mock_llm.chat = AsyncMock(side_effect=Exception("API error"))
        planner = TaskPlanner(mock_llm)
        result = await planner.generate_plan({"title": "fallback task"})
        assert len(result) == 1
        assert result[0]["title"] == "fallback task"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd C:\Users\anime\ares && python -m pytest tests/test_planner.py -x -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement the planner**

Create `ares/planner.py`:

```python
"""LLM-based task planner. Generates execution plans for auto-executable tasks."""

import json
import re
import logging

logger = logging.getLogger(__name__)

PLANNING_PROMPT = """You are a task planner. Break the following task into clear, actionable steps.

Task: {title}
Description: {description}

Return a JSON array of steps. Each step must have:
- "step": number (starting at 1)
- "title": short action description (max 60 chars)
- "description": detailed instructions for this step

Rules:
- 2-8 steps (keep it focused)
- Steps should be sequential and build on each other
- Last step should be saving/writing the final result
- Each step should be completable by running tools
- Return ONLY the JSON array, no other text"""


class TaskPlanner:
    """Generates execution plans for tasks using the session LLM."""

    def __init__(self, llm_client):
        self.llm = llm_client

    async def generate_plan(self, task: dict) -> list[dict]:
        """Generate an execution plan for a task.

        Returns list of step dicts with keys: step, title, description, status.
        Falls back to a single-step plan on parse failure.
        """
        title = task.get("title", "Untitled task")
        description = task.get("description", "") or ""
        prompt = PLANNING_PROMPT.format(title=title, description=description)

        try:
            response = await self.llm.chat([{"role": "user", "content": prompt}])
            return self._parse_plan(response.get("content", ""))
        except Exception as e:
            logger.warning("Planning failed, using single-step fallback: %s", e)
            return self._fallback_plan(task)

    def _parse_plan(self, content: str) -> list[dict]:
        """Parse JSON plan from LLM response."""
        # Try direct JSON parse
        try:
            plan = json.loads(content)
            if isinstance(plan, list):
                return self._validate_plan(plan)
        except json.JSONDecodeError:
            pass

        # Try extracting JSON from markdown code blocks
        match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", content, re.DOTALL)
        if match:
            try:
                plan = json.loads(match.group(1))
                if isinstance(plan, list):
                    return self._validate_plan(plan)
            except json.JSONDecodeError:
                pass

        # Try finding array in content
        match = re.search(r"\[.*\]", content, re.DOTALL)
        if match:
            try:
                plan = json.loads(match.group(0))
                if isinstance(plan, list):
                    return self._validate_plan(plan)
            except json.JSONDecodeError:
                pass

        raise ValueError("Could not parse plan from LLM response")

    def _validate_plan(self, plan: list[dict]) -> list[dict]:
        """Validate and normalize a parsed plan."""
        validated = []
        for i, step in enumerate(plan):
            if not isinstance(step, dict):
                continue
            validated.append({
                "step": step.get("step", i + 1),
                "title": str(step.get("title", f"Step {i + 1}"))[:60],
                "description": str(step.get("description", "")),
                "status": "pending",
            })

        if not validated:
            raise ValueError("Plan is empty after validation")

        # Ensure sequential numbering
        for i, step in enumerate(validated):
            step["step"] = i + 1

        return validated

    def _fallback_plan(self, task: dict) -> list[dict]:
        """Single-step fallback plan."""
        return [{
            "step": 1,
            "title": task.get("title", "Execute task")[:60],
            "description": task.get("description", "") or task.get("title", ""),
            "status": "pending",
        }]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd C:\Users\anime\ares && python -m pytest tests/test_planner.py -x -v`
Expected: All tests PASS

- [ ] **Step 5: Commit**

```bash
git add ares/planner.py tests/test_planner.py
git commit -m "feat(planner): add LLM-based task planner with JSON parsing and fallback"
```

---

## Task 4: TaskState Model

**Files:**
- Modify: `ares/models.py`
- Test: inline in `tests/test_tasks.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/test_tasks.py`:

```python
class TestTaskStateModel:
    def test_task_state_enum_has_all_states(self):
        from ares.models import TaskState
        states = {s.value for s in TaskState}
        assert "queued" in states
        assert "planning" in states
        assert "running" in states
        assert "retrying" in states
        assert "completed" in states
        assert "failed" in states
        assert "cancelled" in states

    def test_task_transitions_defined(self):
        from ares.models import TASK_TRANSITIONS
        assert "queued" in TASK_TRANSITIONS
        assert "planning" in TASK_TRANSITIONS["queued"]
        assert "completed" in TASK_TRANSITIONS["running"]
        assert TASK_TRANSITIONS["completed"] == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd C:\Users\anime\ares && python -m pytest tests/test_tasks.py::TestTaskStateModel -x -v`
Expected: FAIL with `ImportError`

- [ ] **Step 3: Implement the model**

Open `ares/models.py`. Add at the end of the file:

```python
# ── v2: Task States ──────────────────────────────────────────

from enum import Enum


class TaskState(str, Enum):
    QUEUED = "queued"
    PLANNING = "planning"
    RUNNING = "running"
    RETRYING = "retrying"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


TASK_TRANSITIONS = {
    "queued":      ["planning", "cancelled"],
    "planning":    ["running", "failed", "cancelled"],
    "running":     ["completed", "retrying", "failed", "cancelled"],
    "retrying":    ["running", "failed", "cancelled"],
    "completed":   [],
    "failed":      ["queued"],
    "cancelled":   ["queued"],
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd C:\Users\anime\ares && python -m pytest tests/test_tasks.py::TestTaskStateModel -x -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add ares/models.py tests/test_tasks.py
git commit -m "feat(models): add TaskState enum and TASK_TRANSITIONS map"
```

---

## Task 5: TaskExecutor Rewrite

**Files:**
- Rewrite: `ares/task_executor.py`
- Rewrite: `tests/test_task_executor.py`

This is the largest task. Read the current file first to understand the existing structure, then rewrite.

- [ ] **Step 1: Read current file**

Read `ares/task_executor.py` fully to understand the current `_process_task`, `_execute_task`, `_classify_task`, `_notify`, `run_once`, `run`, `start`, `stop` methods.

- [ ] **Step 2: Write failing tests**

Replace `tests/test_task_executor.py` entirely:

```python
"""Tests for the rewritten task executor with planning and step tracking."""

import json
import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from ares.tools.tasks import TaskStore
from ares.tools.dates import now_local_iso


def _make_store(tmp_path):
    return TaskStore(db_path=tmp_path / "test.db")


def _make_task(store, **kwargs):
    defaults = {"title": "test task", "auto_executable": "yes"}
    defaults.update(kwargs)
    return store.create(**defaults)


def _make_executor(store, mock_llm, mock_tool_executor=None):
    """Create a TaskExecutor with mocked dependencies."""
    from ares.task_executor import TaskExecutor

    mock_callback = MagicMock()
    mock_status = MagicMock()

    executor = TaskExecutor(
        task_store=store,
        agent_runner=AsyncMock(return_value={"summary": "done"}),
        callback=mock_callback,
        poll_seconds=1,
        max_turns=5,
        enabled=True,
    )
    executor.status_callback = mock_status

    # Wire LLM and tool executor
    executor.llm = mock_llm
    executor.tool_executor = mock_tool_executor or MagicMock()
    executor.allowed_tools = ["web_search", "read_file", "write_file"]
    executor.planner = None  # set per-test

    return executor


class TestHelperMethods:
    def test_log_event_creates_record(self, tmp_path):
        store = _make_store(tmp_path)
        task_id = _make_task(store)
        executor = _make_executor(store, AsyncMock())
        executor._log_event(task_id, "info", None, "test message")
        events = store.get_events(task_id)
        assert len(events) == 1
        assert events[0]["message"] == "test message"
        assert events[0]["level"] == "info"

    def test_log_event_with_step(self, tmp_path):
        store = _make_store(tmp_path)
        task_id = _make_task(store)
        executor = _make_executor(store, AsyncMock())
        executor._log_event(task_id, "success", 2, "step done")
        events = store.get_events(task_id)
        assert events[0]["step"] == 2

    def test_set_state_updates_task(self, tmp_path):
        store = _make_store(tmp_path)
        task_id = _make_task(store)
        executor = _make_executor(store, AsyncMock())
        executor._set_state(task_id, "running")
        task = store.get(task_id)
        assert task["state"] == "running"

    def test_set_state_logs_event(self, tmp_path):
        store = _make_store(tmp_path)
        task_id = _make_task(store)
        executor = _make_executor(store, AsyncMock())
        executor._set_state(task_id, "planning")
        events = store.get_events(task_id)
        assert any("planning" in e["message"] for e in events)

    def test_format_size(self):
        from ares.task_executor import TaskExecutor
        assert TaskExecutor._format_size(0) == "0.0 B"
        assert TaskExecutor._format_size(1024) == "1.0 KB"
        assert TaskExecutor._format_size(1048576) == "1.0 MB"
        assert TaskExecutor._format_size(1073741824) == "1.0 GB"


class TestExecuteStep:
    @pytest.mark.asyncio
    async def test_execute_step_with_tool_calls(self, tmp_path):
        store = _make_store(tmp_path)
        task_id = _make_task(store)
        task = store.get(task_id)

        mock_llm = AsyncMock()
        mock_llm.chat = AsyncMock(side_effect=[
            {"tool_calls": [{"id": "c1", "tool": "web_search", "args": {"query": "test"}}], "content": None},
            {"content": "Step complete", "tool_calls": None},
        ])

        mock_tool = MagicMock()
        mock_tool.execute = MagicMock(return_value="search results")

        executor = _make_executor(store, mock_llm, mock_tool)
        executor.tool_executor = mock_tool

        step = {"step": 1, "title": "Search", "description": "find stuff", "status": "pending"}
        result = await executor._execute_step(task, step)

        assert result["status"] == "success"
        assert result["tool_calls"] == 1

    @pytest.mark.asyncio
    async def test_execute_step_tracks_artifacts(self, tmp_path):
        store = _make_store(tmp_path)
        task_id = _make_task(store)
        task = store.get(task_id)

        mock_llm = AsyncMock()
        mock_llm.chat = AsyncMock(side_effect=[
            {"tool_calls": [{"id": "c1", "tool": "write_file", "args": {"path": "/tmp/test.md"}}], "content": None},
            {"content": "Done", "tool_calls": None},
        ])

        mock_tool = MagicMock()
        mock_tool.execute = MagicMock(return_value="written")

        executor = _make_executor(store, mock_llm, mock_tool)
        executor.tool_executor = mock_tool

        step = {"step": 1, "title": "Write", "description": "write file", "status": "pending"}
        result = await executor._execute_step(task, step)

        assert len(result["artifacts"]) == 1
        assert result["artifacts"][0]["path"] == "/tmp/test.md"


class TestHandleFailure:
    @pytest.mark.asyncio
    async def test_retry_on_first_failure(self, tmp_path):
        store = _make_store(tmp_path)
        task_id = _make_task(store)
        store.update(task_id, attempt=1, max_attempts=3)
        task = store.get(task_id)

        executor = _make_executor(store, AsyncMock())

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await executor._handle_failure(task_id, task, "timeout")

        task = store.get(task_id)
        assert task["state"] == "queued"
        assert task["attempt"] == 2
        assert task["retry_reason"] == "timeout"

    @pytest.mark.asyncio
    async def test_exhaust_retries(self, tmp_path):
        store = _make_store(tmp_path)
        task_id = _make_task(store)
        store.update(task_id, attempt=3, max_attempts=3)
        task = store.get(task_id)

        executor = _make_executor(store, AsyncMock())
        await executor._handle_failure(task_id, task, "persistent error")

        task = store.get(task_id)
        assert task["state"] == "failed"
        assert "3 attempts" in (task.get("execution_notes") or "")


class TestResumeTask:
    @pytest.mark.asyncio
    async def test_resume_skips_completed_steps(self, tmp_path):
        store = _make_store(tmp_path)
        task_id = _make_task(store)

        plan = [
            {"step": 1, "title": "Step 1", "description": "d1", "status": "completed"},
            {"step": 2, "title": "Step 2", "description": "d2", "status": "pending"},
            {"step": 3, "title": "Step 3", "description": "d3", "status": "pending"},
        ]
        store.update(task_id,
            state="failed",
            plan=json.dumps(plan),
            total_steps=3,
            current_step=2,
            completed_steps=json.dumps([1]),
        )
        task = store.get(task_id)

        mock_llm = AsyncMock()
        mock_llm.chat = AsyncMock(return_value={"content": "Step done", "tool_calls": None})

        executor = _make_executor(store, mock_llm)
        executor.planner = MagicMock()
        executor.planner.generate_plan = AsyncMock(return_value=plan)

        await executor._resume_task(task)

        task = store.get(task_id)
        assert task["state"] == "completed"

    @pytest.mark.asyncio
    async def test_resume_all_completed(self, tmp_path):
        store = _make_store(tmp_path)
        task_id = _make_task(store)

        plan = [{"step": 1, "title": "S1", "description": "d", "status": "completed"}]
        store.update(task_id,
            state="failed",
            plan=json.dumps(plan),
            total_steps=1,
            completed_steps=json.dumps([1]),
        )
        task = store.get(task_id)

        executor = _make_executor(store, AsyncMock())
        await executor._resume_task(task)

        task = store.get(task_id)
        assert task["state"] == "completed"


class TestCompletionReport:
    @pytest.mark.asyncio
    async def test_report_generation(self, tmp_path):
        store = _make_store(tmp_path)
        task_id = _make_task(store)
        store.update(task_id, attempt=1, max_attempts=3)
        task = store.get(task_id)

        report_json = json.dumps({
            "title": "Researched LLMs",
            "summary": "Comprehensive research on LLMs.",
            "key_results": ["Transformers use self-attention"],
        })
        mock_llm = AsyncMock()
        mock_llm.chat = AsyncMock(return_value={"content": report_json})

        executor = _make_executor(store, mock_llm)
        plan = [{"step": 1, "title": "Research", "description": "d", "status": "completed"}]

        report = await executor._generate_completion_report(task, plan, tool_call_count=5)
        assert report["title"] == "Researched LLMs"
        assert report["steps_completed"] == 1
        assert report["tool_calls_made"] == 5
        assert report["status_emoji"] == "✓"

    @pytest.mark.asyncio
    async def test_report_fallback_on_error(self, tmp_path):
        store = _make_store(tmp_path)
        task_id = _make_task(store)
        store.update(task_id, attempt=1, max_attempts=3)
        task = store.get(task_id)

        mock_llm = AsyncMock()
        mock_llm.chat = AsyncMock(side_effect=Exception("LLM error"))

        executor = _make_executor(store, mock_llm)
        plan = [{"step": 1, "title": "Research", "description": "d", "status": "completed"}]

        report = await executor._generate_completion_report(task, plan, tool_call_count=0)
        assert "summary" in report
        assert report["steps_completed"] == 1
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `cd C:\Users\anime\ares && python -m pytest tests/test_task_executor.py -x -v`
Expected: FAIL with various import/attribute errors

- [ ] **Step 4: Rewrite the executor**

Replace the full content of `ares/task_executor.py` with:

```python
"""Background task execution engine with planning, step tracking, and resume."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
from collections.abc import Callable
from typing import Any

from ares.tools.tasks import TaskStore
from ares.tools.dates import now_local_iso

logger = logging.getLogger(__name__)

ALLOWED_TOOLS = {
    "web_search", "fetch_url",
    "read_file", "search_files", "list_directory",
    "glob_pattern", "get_file_info", "head_file", "tail_file",
    "count_lines", "file_tree",
    "search_memory", "store_memory",
    "write_file", "edit_file", "create_directory",
}

EXECUTOR_STATES = {
    "stopped": "Executor is not running",
    "idle": "Waiting for tasks to execute",
    "scanning": "Scanning for auto-executable tasks",
    "running": "Executing a task",
    "planning": "Generating execution plan",
    "disabled": "Executor is disabled in config",
}


class TaskExecutor:
    """Background engine that proactively executes auto-executable tasks.

    Execution pipeline: planning → step-by-step execution → completion report.
    Supports resume from failed steps and retry with exponential backoff.
    """

    def __init__(
        self,
        task_store: TaskStore,
        agent_runner: Callable[[str, int], Any],
        callback: Callable[[dict], Any] | None = None,
        *,
        poll_seconds: int = 300,
        max_turns: int = 10,
        enabled: bool = True,
    ):
        self.task_store = task_store
        self.agent_runner = agent_runner
        self.callback = callback
        self.status_callback: Callable[[], Any] | None = None
        self.poll_seconds = max(1, poll_seconds)
        self.max_turns = max_turns
        self.enabled = enabled
        self._task: asyncio.Task | None = None
        self._state: str = "stopped"
        self._current_task_id: int | None = None
        self._current_task_title: str | None = None
        self._last_error: str | None = None
        self._tasks_completed: int = 0
        self._tasks_failed: int = 0
        self._started_at: str | None = None

        # v2: planner, llm, tool_executor are wired by server after construction
        self.planner = None
        self.llm = None
        self.tool_executor = None
        self.allowed_tools: list[str] = list(ALLOWED_TOOLS)

        # Resume queue
        self._resume_queue: list[int] = []

    # ── Properties ─────────────────────────────────────────────

    @property
    def state(self) -> str:
        return self._state

    @property
    def current_task_id(self) -> int | None:
        return self._current_task_id

    @property
    def current_task_title(self) -> str | None:
        return self._current_task_title

    @property
    def last_error(self) -> str | None:
        return self._last_error

    @property
    def stats(self) -> dict:
        return {
            "state": self._state,
            "enabled": self.enabled,
            "poll_seconds": self.poll_seconds,
            "current_task_id": self._current_task_id,
            "current_task_title": self._current_task_title,
            "last_error": self._last_error,
            "tasks_completed": self._tasks_completed,
            "tasks_failed": self._tasks_failed,
            "started_at": self._started_at,
        }

    def get_state_display(self) -> str:
        base = EXECUTOR_STATES.get(self._state, self._state)
        if self._current_task_title:
            base += f' — currently: "{self._current_task_title}"'
        if self._last_error:
            base += f" — last error: {self._last_error}"
        return base

    def enqueue_resume(self, task_id: int) -> None:
        """Queue a failed task for resume on the next poll cycle."""
        self._resume_queue.append(task_id)

    # ── Helpers ────────────────────────────────────────────────

    def _log_event(self, task_id: int, level: str, step: int | None, message: str):
        """Insert a task event."""
        try:
            self.task_store.add_event(task_id, level=level, step=step, message=message)
        except Exception as e:
            logger.warning("Failed to log event for task %d: %s", task_id, e)

    def _set_state(self, task_id: int, state: str):
        """Update task state and log the transition."""
        self.task_store.update(task_id, state=state)
        self._log_event(task_id, "info", None, f"State → {state}")

    def _track_artifacts(self, task_id: int, artifacts: list[dict], step_num: int):
        """Track files created/modified during a step."""
        for artifact in artifacts:
            entry = self._build_artifact_entry(artifact, step_num)
            try:
                self.task_store.add_artifact(task_id, entry)
            except Exception as e:
                logger.warning("Failed to track artifact for task %d: %s", task_id, e)

    def _build_artifact_entry(self, artifact: dict, step_num: int) -> dict:
        """Enrich artifact with file metadata."""
        path = artifact.get("path", "")
        size = 0
        try:
            stat = os.stat(path)
            size = stat.st_size
        except (OSError, TypeError):
            pass

        line_count = None
        description = None
        if path and path.endswith(('.md', '.txt', '.py', '.js', '.json', '.yaml')):
            try:
                with open(path, 'r', encoding='utf-8', errors='ignore') as f:
                    content = f.read()
                line_count = content.count('\n') + 1
                for line in content.split('\n'):
                    stripped = line.strip()
                    if stripped and not stripped.startswith('#'):
                        description = stripped[:80]
                        break
            except OSError:
                pass

        return {
            "step": step_num,
            "path": path,
            "artifact_type": artifact.get("type", "unknown"),
            "size_bytes": size,
            "size_human": self._format_size(size),
            "line_count": line_count,
            "description": description,
        }

    @staticmethod
    def _format_size(size_bytes: int) -> str:
        """Format bytes to human-readable size."""
        for unit in ('B', 'KB', 'MB', 'GB'):
            if size_bytes < 1024:
                return f"{size_bytes:.1f} {unit}"
            size_bytes /= 1024
        return f"{size_bytes:.1f} TB"

    # ── Core execution ─────────────────────────────────────────

    async def _process_task(self, task: dict) -> None:
        """Process one task: plan → execute steps → completion report."""
        task_id = task["id"]
        title = task["title"]
        self._current_task_id = task_id
        self._current_task_title = title
        self._push_status()
        logger.info("Processing task #%d: %s", task_id, title)

        try:
            # Phase 1: Planning
            self._set_state(task_id, "planning")
            self._log_event(task_id, "info", None, "Generating execution plan...")

            if self.planner:
                plan = await self.planner.generate_plan(task)
            else:
                # Fallback: single step if no planner wired
                plan = [{"step": 1, "title": title, "description": task.get("description", ""), "status": "pending"}]

            self.task_store.update(task_id,
                state="planning",
                plan=json.dumps(plan),
                total_steps=len(plan),
                current_step=0,
                completed_steps=json.dumps([]),
            )
            self._log_event(task_id, "success", None, f"Plan ready: {len(plan)} steps")

            # Phase 2: Execute steps
            await self._execute_steps_from(task, plan, [])

            self._tasks_completed += 1
            logger.info("Task #%d completed successfully", task_id)

        except Exception as e:
            logger.error("Task #%d failed: %s", task_id, e)
            self._last_error = str(e)
            self._tasks_failed += 1

        finally:
            self._current_task_id = None
            self._current_task_title = None
            self._push_status()

    async def _execute_steps_from(self, task: dict, plan: list[dict], completed: list[int]):
        """Execute remaining steps, skipping already-completed ones."""
        task_id = task["id"]
        self._set_state(task_id, "running")
        tool_call_count = 0

        for step in plan:
            step_num = step["step"]
            if step_num in completed:
                continue

            self.task_store.update(task_id, current_step=step_num)
            self._log_event(task_id, "info", step_num, f"Starting: {step['title']}")

            try:
                result = await self._execute_step(task, step)
                tool_call_count += result.get("tool_calls", 0)
                self._log_event(task_id, "success", step_num, f"Completed: {step['title']}")

                completed.append(step_num)
                self.task_store.update(task_id, completed_steps=json.dumps(completed))

                if result.get("artifacts"):
                    self._track_artifacts(task_id, result["artifacts"], step_num)

            except Exception as e:
                self._log_event(task_id, "error", step_num, f"Failed: {step['title']}: {e}")
                return await self._handle_failure(task_id, task, str(e))

        # All steps done — generate completion report
        self._log_event(task_id, "info", None, "Generating completion report...")
        report = await self._generate_completion_report(task, plan, tool_call_count)
        self.task_store.update(task_id,
            state="completed",
            completion_report=json.dumps(report),
            executed_at=now_local_iso(),
        )
        self._log_event(task_id, "success", None, "Task completed successfully")

        # Notify
        await self._notify(task_id, task["title"], "completed", report.get("summary", "Done"))

    async def _execute_step(self, task: dict, step: dict) -> dict:
        """Execute a single plan step via agent loop.

        Returns:
            {"status": "success"|"error", "output": str, "artifacts": list, "tool_calls": int}
        """
        prompt = (
            f"Task: {task['title']}\n"
            f"Step {step['step']}/{task['total_steps']}: {step['title']}\n"
            f"Instructions: {step['description']}\n\n"
            f"Execute this step using available tools. "
            f"When done, provide a brief summary of what you accomplished."
        )

        system_prompt = self._build_system_prompt()
        messages = [system_prompt, {"role": "user", "content": prompt}]
        artifacts = []
        tool_call_count = 0

        for turn in range(task.get("max_turns", self.max_turns)):
            response = await self.llm.chat(messages, tools=self.allowed_tools)

            if response.get("tool_calls"):
                for call in response["tool_calls"]:
                    tool_name = call.get("tool") or call.get("function", {}).get("name", "")
                    tool_args = call.get("args") or call.get("function", {}).get("arguments", {})
                    result = self.tool_executor.execute(tool_name, tool_args)
                    tool_call_count += 1

                    if tool_name in ("write_file", "edit_file", "create_directory"):
                        artifacts.append({
                            "path": tool_args.get("path", ""),
                            "type": tool_name,
                            "timestamp": now_local_iso(),
                        })

                    messages.append({"role": "tool", "tool_call_id": call.get("id", ""), "content": str(result)})
                messages.append({"role": "assistant", "content": None, "tool_calls": response["tool_calls"]})
            else:
                return {
                    "status": "success",
                    "output": response.get("content", ""),
                    "artifacts": artifacts,
                    "tool_calls": tool_call_count,
                }

        return {
            "status": "success",
            "output": "Step completed (max turns reached)",
            "artifacts": artifacts,
            "tool_calls": tool_call_count,
        }

    def _build_system_prompt(self) -> dict:
        """Build the system prompt for step execution."""
        return {
            "role": "system",
            "content": (
                "You are an AI assistant executing a specific step of a task. "
                "Use the available tools to accomplish the step. "
                "When done, provide a brief summary of what you accomplished."
            ),
        }

    async def _handle_failure(self, task_id: int, task: dict, reason: str) -> None:
        """Handle a step failure — retry with exponential backoff or give up."""
        attempt = task.get("attempt", 1)
        max_attempts = task.get("max_attempts", 3)

        if attempt >= max_attempts:
            self._set_state(task_id, "failed")
            self.task_store.update(task_id,
                execution_notes=f"Failed after {attempt} attempts. Last error: {reason}",
                executed_at=now_local_iso(),
            )
            self._log_event(task_id, "error", None, f"Failed after {attempt} attempts: {reason}")
            await self._notify(task_id, task["title"], "failed", f"Failed: {reason}")
            return

        self._log_event(task_id, "warning", None,
            f"Step failed, scheduling retry (attempt {attempt + 1}/{max_attempts}): {reason}")

        self.task_store.update(task_id, state="retrying", retry_reason=reason)

        # Exponential backoff: 5s, 15s, 45s (Temporal pattern)
        delay = min(5 * (3 ** (attempt - 1)), 45)
        await asyncio.sleep(delay)

        self.task_store.update(task_id, state="queued", attempt=attempt + 1)
        self._log_event(task_id, "info", None,
            f"Retrying from step {task.get('current_step', 1)}/{task.get('total_steps', '?')}")

    async def _resume_task(self, task: dict):
        """Resume a failed task from the next uncompleted step."""
        plan = json.loads(task["plan"])
        completed = json.loads(task.get("completed_steps") or "[]")

        next_step = None
        for step in plan:
            if step["step"] not in completed:
                next_step = step
                break

        if next_step is None:
            self._set_state(task["id"], "completed")
            return

        self._log_event(task["id"], "info", None,
            f"Resuming from step {next_step['step']}/{task['total_steps']}: {next_step['title']}")

        self.task_store.update(task["id"],
            state="running",
            attempt=task.get("attempt", 1) + 1,
            retry_reason=None,
        )

        await self._execute_steps_from(task, plan, completed)

    async def _generate_completion_report(self, task: dict, plan: list[dict], tool_call_count: int) -> dict:
        """Generate a polished completion report via LLM."""
        artifacts = self.task_store.get_artifacts(task["id"])

        prompt = (
            "Generate a polished completion report for this task.\n\n"
            f"Task: {task['title']}\n"
            f"Description: {task.get('description', 'N/A')}\n\n"
            f"Steps executed:\n"
            + "\n".join(f"  {s['step']}. {s['title']}" for s in plan) + "\n\n"
            f"Files created: {[a['path'] for a in artifacts if a['artifact_type'] == 'write_file']}\n"
            f"Files modified: {[a['path'] for a in artifacts if a['artifact_type'] == 'edit_file']}\n\n"
            "Return a JSON object:\n"
            '{\n'
            '  "title": "action verb + topic (max 60 chars)",\n'
            '  "summary": "2-3 sentence executive summary",\n'
            '  "key_results": ["bullet 1", "bullet 2", ...],\n'
            '  "file_descriptions": {"path": "one-line description"}\n'
            '}\n\n'
            "Write in a confident, precise tone. Return ONLY the JSON."
        )

        try:
            response = await self.llm.chat([{"role": "user", "content": prompt}])
            report = json.loads(response["content"])
        except (json.JSONDecodeError, KeyError, Exception):
            report = {
                "title": f"Completed: {task['title'][:50]}",
                "summary": f"Task '{task['title']}' completed successfully.",
                "key_results": [f"Completed {len(plan)} steps"],
            }

        report["status_emoji"] = "✓"
        report["status_label"] = "Completed"
        report["steps_completed"] = len(plan)
        report["total_steps"] = len(plan)
        report["tool_calls_made"] = tool_call_count
        report["attempt"] = task.get("attempt", 1)
        report["max_attempts"] = task.get("max_attempts", 3)
        report["files_created"] = [a["path"] for a in artifacts if a["artifact_type"] == "write_file"]
        report["files_modified"] = [a["path"] for a in artifacts if a["artifact_type"] == "edit_file"]

        return report

    # ── Lifecycle (kept for backward compat) ───────────────────

    def _push_status(self) -> None:
        if self.status_callback is not None:
            result = self.status_callback()
            if inspect.isawaitable(result):
                asyncio.ensure_future(result)

    async def _notify(self, task_id: int, title: str, status: str, notes: str) -> None:
        task_info = {"id": task_id, "title": title, "status": status, "notes": notes}
        if self.callback is not None:
            result = self.callback(task_info)
            if inspect.isawaitable(result):
                await result

    async def run_once(self) -> int:
        """Scan and execute all auto-executable tasks once. Returns count processed."""
        if not self.enabled:
            self._state = "disabled"
            return 0

        self._state = "scanning"
        self._push_status()

        # Process any queued resumes first
        while self._resume_queue:
            resume_id = self._resume_queue.pop(0)
            task = self.task_store.get(resume_id)
            if task and task.get("plan"):
                self._current_task_id = resume_id
                self._current_task_title = task["title"]
                self._state = "running"
                self._push_status()
                try:
                    await self._resume_task(task)
                except Exception as e:
                    logger.error("Resume failed for task #%d: %s", resume_id, e)
                finally:
                    self._current_task_id = None
                    self._current_task_title = None

        # Scan for new auto-executable tasks
        tasks = self.task_store.get_auto_executable()
        processed = 0
        for task in tasks:
            state = task.get("state") or task.get("status", "pending")
            if state not in ("pending", "queued"):
                continue
            await self._process_task(task)
            processed += 1
            if processed < len(tasks):
                await asyncio.sleep(5)

        self._state = "idle"
        self._push_status()
        return processed

    async def run(self) -> None:
        self._started_at = now_local_iso()
        while True:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self._last_error = str(e)
                self._state = "idle"
                logger.error("Executor loop error: %s", e)
            await asyncio.sleep(self.poll_seconds)

    def start(self) -> None:
        if self._task is not None:
            return
        if not self.enabled:
            self._state = "disabled"
            return
        self._state = "idle"
        self._task = asyncio.create_task(self.run())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
            self._state = "stopped"
            self._current_task_id = None
            self._current_task_title = None
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd C:\Users\anime\ares && python -m pytest tests/test_task_executor.py -x -v`
Expected: All tests PASS

- [ ] **Step 6: Run full test suite to verify no regressions**

Run: `cd C:\Users\anime\ares && python -m pytest tests/ -x -q`
Expected: All tests PASS (or at least no new failures)

- [ ] **Step 7: Commit**

```bash
git add ares/task_executor.py tests/test_task_executor.py
git commit -m "feat(executor): rewrite with planning, step-by-step execution, resume, retry"
```

---

## Task 6: Tool Definitions

**Files:**
- Modify: `ares/tools/definitions.py`
- Test: `tests/test_task_executor.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/test_task_executor.py`:

```python
class TestToolDefinitions:
    def test_resume_task_definition_exists(self):
        from ares.tools.definitions import get_tool_definitions
        defs = get_tool_definitions()
        names = {d["function"]["name"] for d in defs}
        assert "resume_task" in names

    def test_get_task_events_definition_exists(self):
        from ares.tools.definitions import get_tool_definitions
        defs = get_tool_definitions()
        names = {d["function"]["name"] for d in defs}
        assert "get_task_events" in names

    def test_get_task_artifacts_definition_exists(self):
        from ares.tools.definitions import get_tool_definitions
        defs = get_tool_definitions()
        names = {d["function"]["name"] for d in defs}
        assert "get_task_artifacts" in names
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd C:\Users\anime\ares && python -m pytest tests/test_task_executor.py::TestToolDefinitions -x -v`
Expected: FAIL with `AssertionError`

- [ ] **Step 3: Add tool definitions**

Open `ares/tools/definitions.py`. Add at the end, before the closing of the tool list:

```python
    _tool(
        "resume_task",
        "Resume a failed task from where it left off. Only works on tasks with state='failed'. Re-executes from the first uncompleted step.",
        {
            "task_id": {"type": "integer", "description": "ID of the failed task to resume"},
        },
        required=["task_id"],
    ),
    _tool(
        "get_task_events",
        "Get the execution log for a task. Shows all state changes, step progress, and events with timestamps.",
        {
            "task_id": {"type": "integer", "description": "ID of the task"},
            "limit": {"type": "integer", "description": "Max events to return (default 50)"},
        },
        required=["task_id"],
    ),
    _tool(
        "get_task_artifacts",
        "Get all files created or modified by a task. Shows file paths, sizes, and which step created them.",
        {
            "task_id": {"type": "integer", "description": "ID of the task"},
        },
        required=["task_id"],
    ),
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd C:\Users\anime\ares && python -m pytest tests/test_task_executor.py::TestToolDefinitions -x -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add ares/tools/definitions.py tests/test_task_executor.py
git commit -m "feat(tools): add resume_task, get_task_events, get_task_artifacts definitions"
```

---

## Task 7: ToolExecutor Handlers

**Files:**
- Modify: `ares/tools/executor.py`
- Test: `tests/test_task_executor.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/test_task_executor.py`:

```python
class TestToolExecutorHandlers:
    def _make_executor(self, tmp_path):
        from ares.tools.tasks import TaskStore
        from ares.tools.executor import ToolExecutor
        from unittest.mock import MagicMock
        store = TaskStore(db_path=tmp_path / "test.db")
        te = ToolExecutor(
            memory_store=MagicMock(),
            task_store=store,
            config=MagicMock(),
        )
        return te, store

    def test_resume_task_not_found(self, tmp_path):
        te, _ = self._make_executor(tmp_path)
        result = te.execute("resume_task", {"task_id": 999})
        assert "not found" in result

    def test_resume_task_wrong_state(self, tmp_path):
        te, store = self._make_executor(tmp_path)
        task_id = store.create("test")
        result = te.execute("resume_task", {"task_id": task_id})
        assert "cannot be resumed" in result

    def test_get_task_events_empty(self, tmp_path):
        te, store = self._make_executor(tmp_path)
        task_id = store.create("test")
        result = te.execute("get_task_events", {"task_id": task_id})
        assert "No events" in result

    def test_get_task_events_with_events(self, tmp_path):
        te, store = self._make_executor(tmp_path)
        task_id = store.create("test")
        store.add_event(task_id, "info", None, "started")
        store.add_event(task_id, "success", 1, "step done")
        result = te.execute("get_task_events", {"task_id": task_id})
        assert "started" in result
        assert "step done" in result

    def test_get_task_artifacts_empty(self, tmp_path):
        te, store = self._make_executor(tmp_path)
        task_id = store.create("test")
        result = te.execute("get_task_artifacts", {"task_id": task_id})
        assert "No artifacts" in result
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd C:\Users\anime\ares && python -m pytest tests/test_task_executor.py::TestToolExecutorHandlers -x -v`
Expected: FAIL with `ValueError: Unknown tool`

- [ ] **Step 3: Add handlers to ToolExecutor**

Open `ares/tools/executor.py`. 

First, add `self.task_executor_ref = None` in `__init__` (after line 50):

```python
        self.task_executor_ref = None  # wired by server for resume support
```

Then add these three methods to the `ToolExecutor` class (after the `_terminal_exec` method):

```python
    # ── v2 Task Tools ──────────────────────────────────────────

    def _resume_task(self, args: dict) -> str:
        """Resume a failed task from where it left off."""
        task_id = int(args["task_id"])
        task = self.tasks.get(task_id)

        if not task:
            return f"Task #{task_id} not found."

        state = task.get("state") or task.get("status", "pending")
        if state not in ("failed", "cancelled"):
            return f"Task #{task_id} cannot be resumed (state: {state})."

        if not task.get("plan"):
            return f"Task #{task_id} has no execution plan. Cannot resume."

        if self.task_executor_ref:
            self.task_executor_ref.enqueue_resume(task_id)
            return f"Task #{task_id} queued for resume."
        else:
            return "Task executor not available."

    def _get_task_events(self, args: dict) -> str:
        """Get the execution log for a task."""
        task_id = int(args["task_id"])
        limit = int(args.get("limit", 50))
        events = self.tasks.get_events(task_id, limit=limit)

        if not events:
            return f"No events found for task #{task_id}."

        lines = [f"Execution Log — Task #{task_id}:"]
        for event in events:
            ts = event.get("timestamp", "?")
            level = event.get("level", "info")
            step = event.get("step")
            msg = event.get("message", "")

            icon = {"info": "→", "success": "✓", "warning": "⚠", "error": "✗"}.get(level, "·")
            step_prefix = f"Step {step}: " if step else ""

            lines.append(f"  {icon} {ts}  {step_prefix}{msg}")

        return "\n".join(lines)

    def _get_task_artifacts(self, args: dict) -> str:
        """Get all files created or modified by a task."""
        task_id = int(args["task_id"])
        artifacts = self.tasks.get_artifacts(task_id)

        if not artifacts:
            return f"No artifacts found for task #{task_id}."

        lines = [f"Artifacts — Task #{task_id}:"]
        for a in artifacts:
            icon = "📄" if a["artifact_type"] == "write_file" else "📝" if a["artifact_type"] == "edit_file" else "📁"
            step = a.get("step", "?")
            size = a.get("size_human", "?")
            lines.append(f"  {icon} {a['path']}")
            lines.append(f"     {size}" + (f" · {a['line_count']} lines" if a.get('line_count') else ""))
            lines.append(f"     Step {step}")

        return "\n".join(lines)
```

Finally, add the three handlers to the `handlers` dict in the `execute()` method (around line 54):

```python
            "resume_task": self._resume_task,
            "get_task_events": self._get_task_events,
            "get_task_artifacts": self._get_task_artifacts,
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd C:\Users\anime\ares && python -m pytest tests/test_task_executor.py::TestToolExecutorHandlers -x -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add ares/tools/executor.py tests/test_task_executor.py
git commit -m "feat(executor): add resume_task, get_task_events, get_task_artifacts handlers"
```

---

## Task 8: Server Integration

**Files:**
- Modify: `ares/server.py`

- [ ] **Step 1: Wire planner and LLM to executor**

Open `ares/server.py`. Find where the `TaskExecutor` is created (search for `TaskExecutor(`). After the executor is created, add:

```python
        # v2: Wire planner, LLM, and tool executor
        from ares.planner import TaskPlanner
        from ares.llm import LLMClient
        llm = LLMClient(self.config)
        self.task_executor.planner = TaskPlanner(llm)
        self.task_executor.llm = llm
        self.task_executor.tool_executor = self.agent.tool_executor
        self.agent.tool_executor.task_executor_ref = self.task_executor
```

- [ ] **Step 2: Update `_execute_task_in_background` to support planning**

Find `_execute_task_in_background` in `server.py`. Replace the body to support the new planning + step pipeline. The key change is that the executor now handles planning and steps internally, so the background runner just needs to call `_process_task`:

The existing `_execute_task_in_background` should now delegate to the executor's own pipeline. Update the function to:

```python
    async def _execute_task_in_background(self, task: dict) -> dict:
        """Execute a task using the executor's planning + step pipeline."""
        from ares.planner import TaskPlanner
        from ares.llm import LLMClient

        task_id = task["id"]
        llm = LLMClient(self.config)
        planner = TaskPlanner(llm)

        # Wire dependencies
        self.task_executor.planner = planner
        self.task_executor.llm = llm
        self.task_executor.tool_executor = self.agent.tool_executor

        # Run through the executor pipeline
        await self.task_executor._process_task(task)

        # Return result for callback
        updated_task = self.task_store.get(task_id)
        return {
            "status": updated_task.get("state", "completed"),
            "notes": updated_task.get("execution_notes", "Done"),
        }
```

- [ ] **Step 3: Enhance `_notify_auto_complete` with report**

Find `_notify_auto_complete` in `server.py`. Update to include the completion report:

```python
    def _notify_auto_complete(self, task: dict, result: dict):
        """Notify connected clients of task completion with full report."""
        task_id = task["id"]
        updated_task = self.task_store.get(task_id)

        event = {
            "type": "task_auto_complete",
            "task_id": task_id,
            "state": updated_task.get("state", "completed") if updated_task else "completed",
            "title": task["title"],
            "notes": result.get("notes", ""),
        }

        # Include completion report if available
        if updated_task and updated_task.get("completion_report"):
            try:
                event["report"] = json.loads(updated_task["completion_report"])
            except (json.JSONDecodeError, TypeError):
                pass

        # Send to all connected WebSocket clients
        for ws in self._connected_websockets:
            asyncio.ensure_future(self._send(ws, event))
```

- [ ] **Step 4: Add WebSocket handlers for resume, events, artifacts**

Find the WebSocket message handler in `server.py` (search for `if msg_type ==`). Add handlers for the new message types:

```python
            elif msg_type == "task:resume":
                task_id = data.get("task_id")
                if task_id:
                    result = self.agent.tool_executor.execute("resume_task", {"task_id": task_id})
                    await self._send(ws, {"type": "task:resumed", "task_id": task_id, "message": result})

            elif msg_type == "task:events":
                task_id = data.get("task_id")
                if task_id:
                    events = self.task_store.get_events(task_id, limit=data.get("limit", 50))
                    await self._send(ws, {"type": "task:events", "task_id": task_id, "events": events})

            elif msg_type == "task:artifacts":
                task_id = data.get("task_id")
                if task_id:
                    artifacts = self.task_store.get_artifacts(task_id)
                    await self._send(ws, {"type": "task:artifacts", "task_id": task_id, "artifacts": artifacts})
```

- [ ] **Step 5: Run tests**

Run: `cd C:\Users\anime\ares && python -m pytest tests/ -x -q`
Expected: All tests PASS

- [ ] **Step 6: Commit**

```bash
git add ares/server.py
git commit -m "feat(server): wire planner/LLM to executor, add WebSocket handlers for resume/events/artifacts"
```

---

## Task 9: Backward Compatibility Audit

**Files:**
- Modify: `ares/tools/tasks.py` (update `update` allowed fields)
- Modify: `ares/tools/executor.py` (update handlers to set both state and status)

- [ ] **Step 1: Add new fields to TaskStore.update allowed set**

Open `ares/tools/tasks.py`. Find the `update` method (around line 171). Update the `allowed` set to include v2 fields:

```python
        allowed = {
            "title", "description", "due", "priority", "status",
            "execution_notes", "executed_at", "max_turns", "retry_count",
            "auto_executable", "reminder_at",
            # v2 fields
            "state", "plan", "current_step", "total_steps", "completed_steps",
            "attempt", "max_attempts", "retry_reason", "completion_report",
        }
```

- [ ] **Step 2: Update create_task handler to set state**

Open `ares/tools/executor.py`. Find `_create_task` handler. Update to also set `state='queued'`:

In the `self.tasks.create(...)` call, no change needed (migration defaults new tasks to state='pending'). But after creation, update state:

```python
    def _create_task(self, args: dict) -> str:
        auto_exec = "yes" if args.get("auto_executable", False) else "no"
        max_turns = int(args.get("max_turns", 10))
        max_attempts = int(args.get("max_attempts", 3))
        task_id = self.tasks.create(
            args["title"],
            description=args.get("description"),
            due=args.get("due"),
            priority=args.get("priority", "medium"),
            reminder_at=args.get("reminder_at"),
            auto_executable=auto_exec,
            max_turns=max_turns,
        )
        # Set v2 state
        self.tasks.update(task_id, state="queued", max_attempts=max_attempts)
        task = self.tasks.get(task_id)
        due_str = f" (due: {task['due']})" if task and task.get("due") else ""
        auto_str = " [auto]" if auto_exec == "yes" else ""
        return f"Created task #{task_id}: {args['title']}{due_str}{auto_str}"
```

- [ ] **Step 3: Update complete_task handler to set state**

Find `_complete_task` handler. Update to also set `state='completed'`:

```python
    def _complete_task(self, args: dict) -> str:
        task_id = int(args["task_id"])
        task = self.tasks.get(task_id)
        if not task:
            return f"Task #{task_id} was not found."
        # Set both old status and new state
        self.tasks.update(task_id, state="completed")
        if self.tasks.complete(task_id):
            return f"Completed task #{task_id}."
        return f"Task #{task_id} was not found or is not pending."
```

- [ ] **Step 4: Update cancel_task handler to set state**

Find `_cancel_task` handler. Update to also set `state='cancelled'`:

```python
    def _cancel_task(self, args: dict) -> str:
        task_id = int(args["task_id"])
        self.tasks.update(task_id, state="cancelled")
        if self.tasks.cancel(task_id):
            return f"Cancelled task #{task_id}."
        return f"Task #{task_id} was not found."
```

- [ ] **Step 5: Run full test suite**

Run: `cd C:\Users\anime\ares && python -m pytest tests/ -x -q`
Expected: All tests PASS

- [ ] **Step 6: Commit**

```bash
git add ares/tools/tasks.py ares/tools/executor.py
git commit -m "feat: backward compat — set both state and status in create/complete/cancel handlers"
```

---

## Task 10: Run All Tests and Final Verification

- [ ] **Step 1: Run the full test suite**

Run: `cd C:\Users\anime\ares && python -m pytest tests/ -x -v`
Expected: All tests PASS

- [ ] **Step 2: Verify migration on existing database**

If a real `~/.ares/data/ares.db` exists, verify the migration ran:

```bash
cd C:\Users\anime\ares && python -c "
from ares.tools.tasks import TaskStore
store = TaskStore()
# Check new columns exist
rows = store.conn.execute('PRAGMA table_info(tasks)').fetchall()
cols = {r['name'] for r in rows}
print('state' in cols and 'plan' in cols and 'current_step' in cols)
# Check new tables exist
tables = {r[0] for r in store.conn.execute(\"SELECT name FROM sqlite_master WHERE type='table'\").fetchall()}
print('task_events' in tables and 'task_artifacts' in tables)
print('Migration OK')
"
```

- [ ] **Step 3: Final commit**

```bash
git add -A
git commit -m "feat: Task Execution Engine Phase 1 — planning, steps, resume, retry, events, artifacts, reports"
```

---

## Summary

| Task | What it builds | Tests |
|------|---------------|-------|
| 1 | DB migration (new columns + tables) | 11 tests |
| 2 | TaskStore query methods | 7 tests |
| 3 | TaskPlanner module | 10 tests |
| 4 | TaskState model | 2 tests |
| 5 | TaskExecutor rewrite | 12 tests |
| 6 | Tool definitions | 3 tests |
| 7 | ToolExecutor handlers | 5 tests |
| 8 | Server integration | manual |
| 9 | Backward compatibility | manual |
| 10 | Final verification | full suite |

**Total new tests: ~50**
**Files changed: 7 modified, 2 created**
**Estimated lines: ~1,500**
