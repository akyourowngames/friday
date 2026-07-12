# Proactive Task Execution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a proactive background task execution engine that auto-completes tasks marked as auto-executable, with instant notifications.

**Architecture:** A `TaskExecutor` class runs an async polling loop (following the same pattern as `ReminderService`), scans for tasks with `auto_executable='yes'`, evaluates whether they can be completed via keyword classification, executes them in an isolated agent loop with restricted tools, and notifies the user instantly via callback + desktop notification.

**Tech Stack:** Python asyncio, SQLite (WAL mode already enabled), rich panels for notifications, prompt_toolkit for CLI commands.

---

## File Structure

| File | Action | Responsibility |
|------|--------|----------------|
| `ares/models.py` | Modify | Add `TaskStatus.PARTIAL`, `TaskStatus.IN_PROGRESS` |
| `ares/tasks.py` | Modify | Add 5 new columns via `_ensure_column`, add `update()`, `get_auto_executable()`, `get_recently_executed()` |
| `ares/config.py` | No change | Config fields already in `AppConfig` (added via spec) |
| `ares/models.py` | Modify | Add 4 new `AppConfig` fields |
| `ares/task_executor.py` | Create | Background execution engine: keyword classification, isolated agent loop, notifications |
| `ares/tools.py` | Modify | Add `auto_executable` param to `create_task`, add `get_execution_status` tool |
| `ares/cli.py` | Modify | Add `/tasks auto on|off|list`, `/tasks history`, auto-complete notifications, start executor |
| `ares/server.py` | Modify | Add `task_auto_complete` WebSocket event, start executor |
| `tests/test_task_executor.py` | Create | Tests for TaskExecutor |

---

### Task 1: Extend Task model with new fields

**Files:**
- Modify: `ares/models.py:19-28`
- Modify: `ares/tasks.py:12-48`

- [ ] **Step 1: Add new status values to TaskStatus enum**

In `ares/models.py`, update the `TaskStatus` enum:

```python
class TaskStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    PARTIAL = "partial"
    CANCELLED = "cancelled"
```

- [ ] **Step 2: Add new fields to AppConfig**

In `ares/models.py`, add these fields to the `AppConfig` class at the end:

```python
    task_executor_enabled: bool = True
    task_executor_poll_seconds: int = 300
    task_executor_max_turns: int = 10
    task_executor_max_cost_usd: float = 0.10
```

- [ ] **Step 3: Run existing tests to verify no breakage**

Run: `pytest tests/test_tasks.py tests/test_tools.py -v`
Expected: All existing tests PASS

- [ ] **Step 4: Commit**

```bash
git add ares/models.py
git commit -m "feat(tasks): add in_progress/partial statuses and executor config fields"
```

---

### Task 2: Add new columns to tasks table

**Files:**
- Modify: `ares/tasks.py:27-48`
- Modify: `ares/tasks.py:50-75`

- [ ] **Step 1: Write the failing test for new columns**

Create `tests/test_task_executor.py`:

```python
"""Tests for proactive task execution."""

import pytest
from ares.tasks import TaskStore


def test_task_store_has_auto_executable_column(tmp_path):
    """TaskStore should have auto_executable column."""
    store = TaskStore(db_path=tmp_path / "test.db")
    task_id = store.create("Test task", auto_executable="yes")
    task = store.get(task_id)
    assert task["auto_executable"] == "yes"
    store.close()


def test_task_store_has_execution_notes_column(tmp_path):
    """TaskStore should have execution_notes column."""
    store = TaskStore(db_path=tmp_path / "test.db")
    task_id = store.create("Test task")
    store.update(task_id, execution_notes="Did research, found 3 articles.")
    task = store.get(task_id)
    assert task["execution_notes"] == "Did research, found 3 articles."
    store.close()


def test_task_store_has_executed_at_column(tmp_path):
    """TaskStore should have executed_at column."""
    store = TaskStore(db_path=tmp_path / "test.db")
    task_id = store.create("Test task")
    store.update(task_id, executed_at="2026-06-19T12:00:00")
    task = store.get(task_id)
    assert task["executed_at"] == "2026-06-19T12:00:00"
    store.close()


def test_task_store_has_max_turns_column(tmp_path):
    """TaskStore should have max_turns column with default 10."""
    store = TaskStore(db_path=tmp_path / "test.db")
    task_id = store.create("Test task")
    task = store.get(task_id)
    assert task["max_turns"] == 10
    store.close()


def test_task_store_has_retry_count_column(tmp_path):
    """TaskStore should have retry_count column with default 0."""
    store = TaskStore(db_path=tmp_path / "test.db")
    task_id = store.create("Test task")
    task = store.get(task_id)
    assert task["retry_count"] == 0
    store.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_task_executor.py::test_task_store_has_auto_executable_column -v`
Expected: FAIL — `create()` doesn't accept `auto_executable` keyword

- [ ] **Step 3: Update TaskStore._init_db to add new columns**

In `ares/tasks.py`, inside `_init_db()` after the existing `_ensure_column` calls (after line 47), add:

```python
        _ensure_column(self.conn, "tasks", "auto_executable", "TEXT DEFAULT 'no'")
        _ensure_column(self.conn, "tasks", "execution_notes", "TEXT")
        _ensure_column(self.conn, "tasks", "executed_at", "TEXT")
        _ensure_column(self.conn, "tasks", "max_turns", "INTEGER DEFAULT 10")
        _ensure_column(self.conn, "tasks", "retry_count", "INTEGER DEFAULT 0")
```

- [ ] **Step 4: Update TaskStore.create() to accept new fields**

In `ares/tasks.py`, update the `create` method signature and SQL:

```python
    def create(
        self,
        title: str,
        description: str | None = None,
        due: str | None = None,
        priority: str = "medium",
        reminder_at: str | None = None,
        auto_executable: str = "no",
        max_turns: int = 10,
    ) -> int:
        """Create a new task. Returns the task id."""
        normalized_due = parse_user_datetime(due)
        normalized_reminder = parse_user_datetime(reminder_at) if reminder_at else normalized_due
        cursor = self.conn.execute(
            """INSERT INTO tasks (title, description, due, priority, reminder_at,
               original_due_text, updated_at, auto_executable, max_turns)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                title,
                description,
                normalized_due,
                priority,
                normalized_reminder,
                due if due != normalized_due else None,
                now_local_iso(),
                auto_executable,
                max_turns,
            ),
        )
        self.conn.commit()
        return cursor.lastrowid
```

- [ ] **Step 5: Add TaskStore.update() method**

In `ares/tasks.py`, add this method after the `complete` method (after line 159):

```python
    def update(self, task_id: int, **kwargs) -> bool:
        """Update arbitrary fields on a task. Returns True if successful."""
        allowed = {
            "title", "description", "due", "priority", "status",
            "execution_notes", "executed_at", "max_turns", "retry_count",
            "auto_executable", "reminder_at",
        }
        updates = {k: v for k, v in kwargs.items() if k in allowed}
        if not updates:
            return False
        updates["updated_at"] = now_local_iso()
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [task_id]
        cursor = self.conn.execute(
            f"UPDATE tasks SET {set_clause} WHERE id = ?",
            values,
        )
        self.conn.commit()
        return cursor.rowcount > 0
```

- [ ] **Step 6: Add TaskStore.get_auto_executable() method**

In `ares/tasks.py`, add this method after `get_due_soon`:

```python
    def get_auto_executable(self) -> list[dict]:
        """Get pending tasks marked as auto_executable."""
        rows = self.conn.execute(
            """SELECT * FROM tasks
               WHERE status = 'pending' AND auto_executable = 'yes'
               ORDER BY due IS NULL, due ASC"""
        ).fetchall()
        return [dict(r) for r in rows]
```

- [ ] **Step 7: Add TaskStore.get_recently_executed() method**

In `ares/tasks.py`, add this method after `get_auto_executable`:

```python
    def get_recently_executed(self, limit: int = 10) -> list[dict]:
        """Get tasks that were auto-executed (done or partial), ordered by most recent."""
        rows = self.conn.execute(
            """SELECT * FROM tasks
               WHERE executed_at IS NOT NULL
               ORDER BY executed_at DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
```

- [ ] **Step 8: Run all tests to verify**

Run: `pytest tests/test_task_executor.py -v`
Expected: All 5 tests PASS

- [ ] **Step 9: Commit**

```bash
git add ares/tasks.py tests/test_task_executor.py
git commit -m "feat(tasks): add auto_executable, execution_notes, executed_at, max_turns, retry_count columns"
```

---

### Task 3: Build the TaskExecutor background engine

**Files:**
- Create: `ares/task_executor.py`

- [ ] **Step 1: Write the failing test for keyword classification**

Add to `tests/test_task_executor.py`:

```python
from ares.task_executor import TaskExecutor


def test_classify_task_research():
    """Tasks with research keywords should be classified as research."""
    executor = TaskExecutor.__new__(TaskExecutor)  # skip __init__
    assert executor._classify_task("Research Python async patterns") == "research"
    assert executor._classify_task("Find out about database migrations") == "research"
    assert executor._classify_task("Look up the best testing frameworks") == "research"
    assert executor._classify_task("What is the capital of France") == "research"
    assert executor._classify_task("Search for articles about AI safety") == "research"


def test_classify_task_file():
    """Tasks with file keywords should be classified as file."""
    executor = TaskExecutor.__new__(TaskExecutor)
    assert executor._classify_task("Create file called notes.md") == "file"
    assert executor._classify_task("Find the config file") == "file"
    assert executor._classify_task("List files in the project") == "file"


def test_classify_task_memory():
    """Tasks with memory keywords should be classified as memory."""
    executor = TaskExecutor.__new__(TaskExecutor)
    assert executor._classify_task("Remind me about the meeting") == "memory"
    assert executor._classify_task("What did I say about the project") == "memory"


def test_classify_task_unknown():
    """Tasks with no matching keywords should be classified as unknown."""
    executor = TaskExecutor.__new__(TaskExecutor)
    assert executor._classify_task("Buy groceries") is None
    assert executor._classify_task("Call the dentist") is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_task_executor.py::test_classify_task_research -v`
Expected: FAIL — `TaskExecutor` not defined

- [ ] **Step 3: Create TaskExecutor with classification logic**

Create `ares/task_executor.py`:

```python
"""Background task execution engine for proactive task completion."""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Callable
from typing import Any

from ares.tasks import TaskStore
from ares.dates import now_local_iso

logger = logging.getLogger(__name__)

# Keywords for task classification — no LLM calls needed for the decision
CATEGORY_KEYWORDS: dict[str, list[str]] = {
    "research": [
        "research", "find out", "look up", "search for",
        "what is", "how to", "investigate", "compare",
    ],
    "file": [
        "create file", "write file", "find file", "list files",
        "check file", "read file", "open file",
    ],
    "memory": [
        "remind me", "what did i say", "recall", "remember",
        "what do i know about", "summarize what",
    ],
}

# Tools allowed for auto-execution (read-only + memory)
ALLOWED_TOOLS = {
    "web_search", "fetch_url",
    "read_file", "search_files", "list_directory",
    "glob_pattern", "get_file_info", "head_file", "tail_file",
    "count_lines", "file_tree",
    "search_memory", "store_memory",
}

# Maximum retry count before giving up
MAX_RETRIES = 3


class TaskExecutor:
    """Background engine that proactively executes auto-executable tasks."""

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
        self.poll_seconds = max(1, poll_seconds)
        self.max_turns = max_turns
        self.enabled = enabled
        self._task: asyncio.Task | None = None

    def _classify_task(self, title: str) -> str | None:
        """Classify a task by keywords. Returns category name or None."""
        lower_title = title.lower()
        for category, keywords in CATEGORY_KEYWORDS.items():
            for keyword in keywords:
                if keyword in lower_title:
                    return category
        return None

    async def _execute_task(self, task: dict) -> dict:
        """Execute a single task. Returns result dict with status and notes."""
        task_id = task["id"]
        title = task["title"]
        description = task.get("description") or ""
        max_turns = task.get("max_turns") or self.max_turns

        # Build the execution prompt
        prompt = f"Complete this task: {title}."
        if description:
            prompt += f" {description}"

        try:
            result = await self.agent_runner(prompt, max_turns)
            return {
                "status": "done",
                "notes": result.get("summary", "Task completed."),
            }
        except Exception as e:
            logger.error("Task execution failed for #%d: %s", task_id, e)
            return {
                "status": "partial",
                "notes": f"Execution failed: {e}",
            }

    async def _process_task(self, task: dict) -> None:
        """Process one task: classify, execute, update, notify."""
        task_id = task["id"]
        title = task["title"]

        # Check retry limit
        retry_count = task.get("retry_count", 0) or 0
        if retry_count >= MAX_RETRIES:
            self.task_store.update(
                task_id,
                status="partial",
                execution_notes=f"Failed after {MAX_RETRIES} retries. Manual intervention needed.",
                executed_at=now_local_iso(),
            )
            return

        # Set to in_progress
        self.task_store.update(task_id, status="in_progress")

        # Classify
        category = self._classify_task(title)
        if category is None:
            self.task_store.update(
                task_id,
                status="partial",
                execution_notes="Task type not recognized for auto-execution.",
                executed_at=now_local_iso(),
            )
            self._notify(task_id, title, "partial", "Task type not recognized for auto-execution.")
            return

        # Execute
        result = await self._execute_task(task)

        # Update task
        now = now_local_iso()
        self.task_store.update(
            task_id,
            status=result["status"],
            execution_notes=result["notes"],
            executed_at=now,
        )

        # Notify
        self._notify(task_id, title, result["status"], result["notes"])

    def _notify(self, task_id: int, title: str, status: str, notes: str) -> None:
        """Notify the user of a task completion."""
        task_info = {
            "id": task_id,
            "title": title,
            "status": status,
            "notes": notes,
        }
        if self.callback is not None:
            result = self.callback(task_info)
            if inspect.isawaitable(result):
                # Fire-and-forget for async callbacks
                asyncio.ensure_future(result)

    async def run_once(self) -> int:
        """Scan and execute all auto-executable tasks once. Returns count processed."""
        if not self.enabled:
            return 0

        tasks = self.task_store.get_auto_executable()
        processed = 0
        for task in tasks:
            await self._process_task(task)
            processed += 1
            # Cooldown between tasks to prevent API rate limiting
            if processed < len(tasks):
                await asyncio.sleep(5)

        return processed

    async def run(self) -> None:
        """Run the executor loop until cancelled."""
        while True:
            try:
                await self.run_once()
            except Exception as e:
                logger.error("Task executor loop error: %s", e)
            await asyncio.sleep(self.poll_seconds)

    def start(self) -> None:
        """Start the background loop as an asyncio task."""
        if self._task is not None:
            return  # Already running
        self._task = asyncio.create_task(self.run())

    async def stop(self) -> None:
        """Stop the background loop gracefully."""
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
```

- [ ] **Step 4: Run classification tests**

Run: `pytest tests/test_task_executor.py -k classify -v`
Expected: All 4 classification tests PASS

- [ ] **Step 5: Write integration test for TaskExecutor run_once**

Add to `tests/test_task_executor.py`:

```python
import asyncio
from unittest.mock import AsyncMock, MagicMock


def test_run_once_processes_auto_executable_tasks(tmp_path):
    """run_once should process tasks marked auto_executable."""
    store = TaskStore(db_path=tmp_path / "test.db")
    store.create("Research AI trends", auto_executable="yes")
    store.create("Buy groceries", auto_executable="no")

    executed = []

    async def fake_runner(prompt, max_turns):
        executed.append(prompt)
        return {"summary": "Found 3 articles."}

    def fake_callback(task_info):
        pass

    executor = TaskExecutor(
        task_store=store,
        agent_runner=fake_runner,
        callback=fake_callback,
        enabled=True,
    )

    count = asyncio.get_event_loop().run_until_complete(executor.run_once())
    assert count == 1
    assert len(executed) == 1
    assert "Research AI trends" in executed[0]

    task = store.get(1)
    assert task["status"] == "done"
    assert "Found 3 articles" in task["execution_notes"]
    assert task["executed_at"] is not None
    store.close()


def test_run_once_skips_unknown_tasks(tmp_path):
    """Tasks with no matching keywords should be marked partial."""
    store = TaskStore(db_path=tmp_path / "test.db")
    store.create("Buy groceries", auto_executable="yes")

    async def fake_runner(prompt, max_turns):
        return {"summary": "Done"}

    executor = TaskExecutor(
        task_store=store,
        agent_runner=fake_runner,
        callback=None,
        enabled=True,
    )

    count = asyncio.get_event_loop().run_until_complete(executor.run_once())
    assert count == 1

    task = store.get(1)
    assert task["status"] == "partial"
    assert "not recognized" in task["execution_notes"]
    store.close()


def test_run_once_disabled_does_nothing(tmp_path):
    """When disabled, run_once should process 0 tasks."""
    store = TaskStore(db_path=tmp_path / "test.db")
    store.create("Research something", auto_executable="yes")

    executor = TaskExecutor(
        task_store=store,
        agent_runner=AsyncMock(),
        enabled=False,
    )

    count = asyncio.get_event_loop().run_until_complete(executor.run_once())
    assert count == 0
    store.close()


def test_run_once_retries_on_failure(tmp_path):
    """Tasks that fail should be retried up to MAX_RETRIES."""
    store = TaskStore(db_path=tmp_path / "test.db")
    store.create("Research AI trends", auto_executable="yes")

    call_count = 0

    async def failing_runner(prompt, max_turns):
        nonlocal call_count
        call_count += 1
        raise RuntimeError("API error")

    executor = TaskExecutor(
        task_store=store,
        agent_runner=failing_runner,
        enabled=True,
    )

    # First run: should set to partial (not done)
    asyncio.get_event_loop().run_until_complete(executor.run_once())
    task = store.get(1)
    assert task["status"] == "partial"
    assert call_count == 1

    # Second and third run: should retry
    asyncio.get_event_loop().run_until_complete(executor.run_once())
    asyncio.get_event_loop().run_until_complete(executor.run_once())
    assert call_count == 3

    # Fourth run: should hit retry limit and set partial
    asyncio.get_event_loop().run_until_complete(executor.run_once())
    task = store.get(1)
    assert task["execution_notes"] == "Failed after 3 retries. Manual intervention needed."
    store.close()
```

- [ ] **Step 6: Run all executor tests**

Run: `pytest tests/test_task_executor.py -v`
Expected: All tests PASS

- [ ] **Step 7: Commit**

```bash
git add ares/task_executor.py tests/test_task_executor.py
git commit -m "feat(executor): add TaskExecutor with keyword classification and background loop"
```

---

### Task 4: Update agent tools for auto-execution

**Files:**
- Modify: `ares/tools.py:85-99` (create_task definition)
- Modify: `ares/tools.py:349-398` (ToolExecutor class)
- Modify: `ares/tools.py:449-459` (_create_task method)

- [ ] **Step 1: Write the failing test for create_task auto_executable param**

Add to `tests/test_task_executor.py`:

```python
def test_create_task_with_auto_executable(tmp_path):
    """create_task tool should accept auto_executable parameter."""
    from ares.tools import ToolExecutor
    from ares.memory import MemoryStore
    from ares.tasks import TaskStore

    memory = MemoryStore(db_path=tmp_path / "memory.db")
    tasks = TaskStore(db_path=tmp_path / "tasks.db")
    executor = ToolExecutor(memory_store=memory, task_store=tasks)

    result = executor.execute("create_task", {
        "title": "Research Python async patterns",
        "auto_executable": True,
    })
    assert "Created task" in result

    task = tasks.get(1)
    assert task["auto_executable"] == "yes"
    memory.close()
    tasks.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_task_executor.py::test_create_task_with_auto_executable -v`
Expected: FAIL — `_create_task` doesn't pass `auto_executable` to `tasks.create()`

- [ ] **Step 3: Update create_task tool definition**

In `ares/tools.py`, update the `create_task` tool definition (around line 85-99):

```python
        _tool(
            "create_task",
            "Create a reminder, to-do, or task.",
            {
                "title": {"type": "string", "description": "The task title."},
                "description": {"type": "string"},
                "due": {"type": "string", "description": "ISO or natural-language due date."},
                "reminder_at": {"type": "string", "description": "ISO or natural-language reminder time."},
                "priority": {
                    "type": "string",
                    "enum": ["low", "medium", "high"],
                    "default": "medium",
                },
                "auto_executable": {
                    "type": "boolean",
                    "default": False,
                    "description": "If true, Ares will try to auto-complete this task in the background.",
                },
                "max_turns": {
                    "type": "integer",
                    "default": 10,
                    "description": "Max tool-use turns for auto-execution (default 10).",
                },
            },
            ["title"],
        ),
```

- [ ] **Step 4: Update _create_task handler**

In `ares/tools.py`, update the `_create_task` method:

```python
    def _create_task(self, args: dict) -> str:
        auto_exec = "yes" if args.get("auto_executable", False) else "no"
        max_turns = int(args.get("max_turns", 10))
        task_id = self.tasks.create(
            args["title"],
            description=args.get("description"),
            due=args.get("due"),
            priority=args.get("priority", "medium"),
            reminder_at=args.get("reminder_at"),
            auto_executable=auto_exec,
            max_turns=max_turns,
        )
        task = self.tasks.get(task_id)
        due_str = f" (due: {task['due']})" if task and task.get("due") else ""
        auto_str = " [auto]" if auto_exec == "yes" else ""
        return f"Created task #{task_id}: {args['title']}{due_str}{auto_str}"
```

- [ ] **Step 5: Add get_execution_status tool definition**

In `ares/tools.py`, add this tool definition in `get_tool_definitions()` after `get_due_soon`:

```python
        _tool(
            "get_execution_status",
            "Show recently auto-completed tasks with execution notes.",
            {"limit": {"type": "integer", "default": 10}},
        ),
```

- [ ] **Step 6: Add _get_execution_status handler**

In `ares/tools.py`, add this handler to `ToolExecutor`:

```python
    def _get_execution_status(self, args: dict) -> str:
        limit = int(args.get("limit", 10))
        tasks = self.tasks.get_recently_executed(limit=limit)
        if not tasks:
            return "No tasks have been auto-executed yet."
        lines = [f"Recently executed ({len(tasks)} task(s)):"]
        for t in tasks:
            status_icon = "✅" if t["status"] == "done" else "⚠️"
            lines.append(
                f"  {status_icon} #{t['id']} {t['title']} [{t['status']}]"
            )
            if t.get("execution_notes"):
                lines.append(f"     Notes: {t['execution_notes']}")
            if t.get("executed_at"):
                lines.append(f"     At: {t['executed_at']}")
        return "\n".join(lines)
```

- [ ] **Step 7: Register the handler in execute()**

In `ares/tools.py`, add `"get_execution_status": self._get_execution_status` to the `handlers` dict in `execute()` (around line 366-398):

```python
            "get_execution_status": self._get_execution_status,
```

- [ ] **Step 8: Run tests**

Run: `pytest tests/test_task_executor.py tests/test_tools.py -v`
Expected: All tests PASS

- [ ] **Step 9: Commit**

```bash
git add ares/tools.py
git commit -m "feat(tools): add auto_executable to create_task, add get_execution_status tool"
```

---

### Task 5: Add CLI commands for auto-execution

**Files:**
- Modify: `ares/cli.py:43-48` (COMPLETER)
- Modify: `ares/cli.py:63-123` (AresCLI.__init__)
- Modify: `ares/cli.py:270-316` (_handle_command /tasks branch)
- Modify: `ares/cli.py:520-523` (run method)
- Modify: `ares/cli.py:550-554` (finally cleanup)

- [ ] **Step 1: Add TaskExecutor import to cli.py**

In `ares/cli.py`, add to imports (after line 33):

```python
from ares.task_executor import TaskExecutor
```

- [ ] **Step 2: Initialize TaskExecutor in __init__**

In `ares/cli.py`, after `self.reminder_service` initialization (after line 113), add:

```python
        self.task_executor = TaskExecutor(
            self.task_store,
            self._execute_taskInBackground,
            self._notify_auto_complete,
            poll_seconds=self.config.task_executor_poll_seconds,
            max_turns=self.config.task_executor_max_turns,
            enabled=self.config.task_executor_enabled,
        )
        self._executor_task: asyncio.Task | None = None
```

- [ ] **Step 3: Add _execute_taskInBackground method**

In `ares/cli.py`, add this method after `_notify_reminder` (after line 165):

```python
    async def _execute_taskInBackground(self, prompt: str, max_turns: int) -> dict:
        """Run an isolated agent loop for background task execution."""
        from ares.llm import LLMClient

        llm = LLMClient(
            api_key=self.config.api_key,
            base_url=self.config.api_base_url,
            model=self.config.model,
        )
        try:
            from ares.tools import get_tool_definitions, ToolExecutor

            tools = get_tool_definitions()
            # Filter to only allowed tools
            from ares.task_executor import ALLOWED_TOOLS
            allowed_defs = [t for t in tools if t["function"]["name"] in ALLOWED_TOOLS]

            messages = [{"role": "user", "content": prompt}]
            summary_parts = []

            for _ in range(max_turns):
                response = await llm.chat(messages, tools=allowed_defs)
                if response.get("tool_calls"):
                    messages.append({
                        "role": "assistant",
                        "content": response.get("content") or "",
                        "tool_calls": response["tool_calls"],
                    })
                    executor = ToolExecutor(
                        memory_store=self.memory_store,
                        task_store=self.task_store,
                        conversation_store=self.conversation_store,
                    )
                    for call in response["tool_calls"]:
                        fn = call["function"]
                        args = __import__("json").loads(fn.get("arguments") or "{}")
                        result = executor.execute(fn["name"], args)
                        messages.append({
                            "tool_call_id": call.get("id", ""),
                            "role": "tool",
                            "content": result,
                        })
                        summary_parts.append(f"{fn['name']}: {result[:200]}")
                else:
                    content = response.get("content", "")
                    if content:
                        summary_parts.append(content)
                    break

            return {"summary": "\n".join(summary_parts) if summary_parts else "Task completed."}
        finally:
            await llm.close()
```

- [ ] **Step 4: Add _notify_auto_complete method**

In `ares/cli.py`, add this method after `_notify_reminder`:

```python
    def _notify_auto_complete(self, task_info: dict) -> None:
        """Render an in-terminal auto-completion notification."""
        status = task_info.get("status", "done")
        icon = "✅" if status == "done" else "⚠️"
        label = "Fully completed" if status == "done" else "Partially completed"
        notes = task_info.get("notes", "")
        notes_str = f"\n[dim]Notes: {notes}[/dim]" if notes else ""
        self.console.print()
        self.console.print(Panel(
            f"[bold yellow]⚡ Auto-completed: {task_info['title']}[/bold yellow]\n"
            f"{icon} {label}{notes_str}",
            border_style="yellow",
            padding=(0, 1),
        ))
        self.console.print()
```

- [ ] **Step 5: Update COMPLETER with new commands**

In `ares/cli.py`, update the COMPLETER list (line 43-48):

```python
COMPLETER = WordCompleter([
    "/help", "/tasks", "/memory", "/model", "/clear",
    "/forget", "/export", "/import", "/reset", "/exit",
    "/soul", "/profile", "/context",
    "/tasks auto on", "/tasks auto off", "/tasks auto list", "/tasks history",
], ignore_case=True)
```

- [ ] **Step 6: Update /tasks command handler**

In `ares/cli.py`, update the `/tasks` command handling in `_handle_command` (lines 295-315). Replace the entire elif block:

```python
        elif command == "/tasks":
            if not arg:
                self._print_tasks(self.task_store.list_pending(), "Pending Tasks")
            elif arg == "all":
                self._print_tasks(self.task_store.list_all(include_done=True), "All Tasks")
            elif arg.startswith("search "):
                self._print_tasks(self.task_store.search(arg[7:].strip(), include_done=True), "Task Search")
            elif arg.startswith("complete "):
                task_id = int(arg.split(maxsplit=1)[1])
                if self.task_store.complete(task_id):
                    self.console.print(f"[green]Completed task #{task_id}.[/green]")
                else:
                    self.console.print(f"[red]Task #{task_id} was not found or is not pending.[/red]")
            elif arg.startswith("cancel "):
                task_id = int(arg.split(maxsplit=1)[1])
                if self.task_store.cancel(task_id):
                    self.console.print(f"[yellow]Cancelled task #{task_id}.[/yellow]")
                else:
                    self.console.print(f"[red]Task #{task_id} was not found.[/red]")
            elif arg.startswith("auto on "):
                task_id = int(arg.split(maxsplit=2)[2])
                if self.task_store.update(task_id, auto_executable="yes"):
                    self.console.print(f"[green]Task #{task_id} marked for auto-execution.[/green]")
                else:
                    self.console.print(f"[red]Task #{task_id} was not found.[/red]")
            elif arg.startswith("auto off "):
                task_id = int(arg.split(maxsplit=2)[2])
                if self.task_store.update(task_id, auto_executable="no"):
                    self.console.print(f"[yellow]Task #{task_id} removed from auto-execution.[/yellow]")
                else:
                    self.console.print(f"[red]Task #{task_id} was not found.[/red]")
            elif arg == "auto list":
                tasks = self.task_store.get_auto_executable()
                if not tasks:
                    self.console.print("[dim]No tasks marked for auto-execution.[/dim]")
                else:
                    self._print_tasks(tasks, "Auto-Executable Tasks")
            elif arg == "history":
                tasks = self.task_store.get_recently_executed()
                if not tasks:
                    self.console.print("[dim]No tasks have been auto-executed yet.[/dim]")
                else:
                    table = Table(title="Execution History", border_style="yellow")
                    table.add_column("ID", style="dim")
                    table.add_column("Title")
                    table.add_column("Status")
                    table.add_column("Notes")
                    table.add_column("Executed At", style="dim")
                    for t in tasks:
                        table.add_row(
                            str(t["id"]),
                            t["title"],
                            t.get("status", "pending"),
                            (t.get("execution_notes") or "")[:60],
                            t.get("executed_at") or "—",
                        )
                    self.console.print(table)
            else:
                self.console.print("[red]Usage: /tasks [all|search QUERY|complete ID|cancel ID|auto on|off|list|history][/red]")
```

- [ ] **Step 7: Start executor in run() method**

In `ares/cli.py`, update the `run()` method (line 520-523):

```python
    async def run(self):
        """Main CLI loop."""
        self._reminder_task = asyncio.create_task(self.reminder_service.run())
        self._executor_task = asyncio.create_task(self.task_executor.run())
        self._show_banner()
```

- [ ] **Step 8: Stop executor in finally block**

In `ares/cli.py`, update the finally block (lines 550-554) to also cancel the executor:

```python
        finally:
            if self._reminder_task is not None:
                self._reminder_task.cancel()
                with suppress(asyncio.CancelledError):
                    await self._reminder_task
            if self._executor_task is not None:
                self._executor_task.cancel()
                with suppress(asyncio.CancelledError):
                    await self._executor_task
```

- [ ] **Step 9: Run tests**

Run: `pytest tests/test_cli.py tests/test_task_executor.py -v`
Expected: All tests PASS

- [ ] **Step 10: Commit**

```bash
git add ares/cli.py
git commit -m "feat(cli): add /tasks auto on|off|list, /tasks history, start executor in background"
```

---

### Task 6: Add WebSocket event for auto-completion

**Files:**
- Modify: `ares/server.py:73-101` (AresServer.__init__)
- Modify: `ares/server.py:356-371` (close method)

- [ ] **Step 1: Add TaskExecutor to AresServer**

In `ares/server.py`, add import (after line 8):

```python
from ares.task_executor import TaskExecutor
```

- [ ] **Step 2: Initialize TaskExecutor in AresServer.__init__**

In `ares/server.py`, after `self.agent = agent or Agent(...)` block (after line 97), add:

```python
        self.task_executor = TaskExecutor(
            self.task_store,
            self._execute_task_in_background,
            self._notify_auto_complete,
            poll_seconds=self.config.task_executor_poll_seconds,
            max_turns=self.config.task_executor_max_turns,
            enabled=self.config.task_executor_enabled,
        )
        self._connected_websockets: list = []
```

- [ ] **Step 3: Add _execute_task_in_background method**

In `ares/server.py`, add this method to `AresServer`:

```python
    async def _execute_task_in_background(self, prompt: str, max_turns: int) -> dict:
        """Run an isolated agent loop for background task execution."""
        from ares.llm import LLMClient
        from ares.tools import get_tool_definitions, ToolExecutor
        from ares.task_executor import ALLOWED_TOOLS
        import json as _json

        llm = LLMClient(
            api_key=self.config.api_key,
            base_url=self.config.api_base_url,
            model=self.config.model,
        )
        try:
            tools = get_tool_definitions()
            allowed_defs = [t for t in tools if t["function"]["name"] in ALLOWED_TOOLS]
            messages = [{"role": "user", "content": prompt}]
            summary_parts = []

            for _ in range(max_turns):
                response = await llm.chat(messages, tools=allowed_defs)
                if response.get("tool_calls"):
                    messages.append({
                        "role": "assistant",
                        "content": response.get("content") or "",
                        "tool_calls": response["tool_calls"],
                    })
                    executor = ToolExecutor(
                        memory_store=self.memory_store,
                        task_store=self.task_store,
                        conversation_store=self.conversation_store,
                    )
                    for call in response["tool_calls"]:
                        fn = call["function"]
                        args = _json.loads(fn.get("arguments") or "{}")
                        result = executor.execute(fn["name"], args)
                        messages.append({
                            "tool_call_id": call.get("id", ""),
                            "role": "tool",
                            "content": result,
                        })
                        summary_parts.append(f"{fn['name']}: {result[:200]}")
                else:
                    content = response.get("content", "")
                    if content:
                        summary_parts.append(content)
                    break
            return {"summary": "\n".join(summary_parts) if summary_parts else "Task completed."}
        finally:
            await llm.close()
```

- [ ] **Step 4: Add _notify_auto_complete method**

In `ares/server.py`, add this method to `AresServer`:

```python
    def _notify_auto_complete(self, task_info: dict) -> None:
        """Send task_auto_complete event to all connected websockets."""
        event = {
            "type": "task_auto_complete",
            "task_id": task_info.get("id"),
            "title": task_info.get("title"),
            "status": task_info.get("status"),
            "notes": task_info.get("notes", ""),
        }
        # Schedule sending to all connected websockets
        for ws in list(self._connected_websockets):
            try:
                asyncio.ensure_future(self._send(ws, event))
            except Exception:
                pass
```

- [ ] **Step 5: Track connected websockets**

In `ares/server.py`, update `handle_client` to track connections:

```python
    async def handle_client(self, websocket: ServerConnection) -> None:
        """Handle a connected desktop renderer."""
        self._connected_websockets.append(websocket)
        try:
            await self._send(websocket, self._session_info())
            await self._send(websocket, self._status())
            async for raw in websocket:
                await self.handle_message(websocket, raw)
        except ConnectionClosed:
            pass
        finally:
            if websocket in self._connected_websockets:
                self._connected_websockets.remove(websocket)
```

- [ ] **Step 6: Start executor in run_forever**

In `ares/server.py`, update `run_forever`:

```python
    async def run_forever(self) -> None:
        """Start the WebSocket server and block until cancelled."""
        self.task_executor.start()
        async with serve(self.handle_client, self.host, self.port) as ws_server:
            self._server = ws_server
            print(f"Ares local API listening on ws://{self.host}:{self.port}")
            await asyncio.Future()
```

- [ ] **Step 7: Stop executor in close**

In `ares/server.py`, update `close`:

```python
    async def close(self) -> None:
        """Shut down stores."""
        await self.task_executor.stop()
        for obj in (
            self.agent,
            self.conversation_store,
            self.memory_store,
            self.task_store,
        ):
            close = getattr(obj, "close", None)
            if close:
                with suppress(Exception):
                    result = close()
                    if result is not None:
                        await result
```

- [ ] **Step 8: Run tests**

Run: `pytest tests/test_server.py tests/test_task_executor.py -v`
Expected: All tests PASS

- [ ] **Step 9: Commit**

```bash
git add ares/server.py
git commit -m "feat(server): add task_auto_complete WebSocket event and background executor"
```

---

### Task 7: Update system prompt for auto-execution

**Files:**
- Modify: `ares/prompts.py:1-50`

- [ ] **Step 1: Update SYSTEM_PROMPT**

In `ares/prompts.py`, update the system prompt to document the new auto-execution feature. After the existing tool list, add:

```python
SYSTEM_PROMPT = """You are Ares, a personal AI assistant living in the user's terminal.
You are like Jarvis from Iron Man — you know the user, remember their preferences,
and help them with daily tasks through natural language.

## Your Capabilities

You have access to these tools:
- **store_memory**: Save facts, preferences, and information the user wants you to remember.
- **search_memory**: Retrieve previously stored information about the user.
- **update_memory**: Correct or enrich an existing memory.
- **delete_memory**: Forget a stored memory by ID.
- **create_task**: Create reminders, to-dos, and tasks. Use `auto_executable: true` for tasks you can complete autonomously (research, file operations, memory compilation).
- **list_tasks**: Show the user their pending tasks.
- **search_tasks**: Find matching tasks.
- **complete_task**: Mark a task done.
- **cancel_task**: Cancel a task.
- **get_due_soon**: Show tasks due soon.
- **get_execution_status**: Show recently auto-completed tasks with execution notes.
- **export_data**: Export local memories, tasks, and conversations to JSON.

## Proactive Task Execution

You can mark tasks as auto-executable when creating them. Ares will then:
1. Run tasks in the background without user interaction
2. Use only safe, read-only tools (web search, file reading, memory)
3. Notify the user when tasks complete or partially complete
4. Log what was done and what remains for manual follow-up

When creating tasks that you could complete yourself (research, finding files, recalling memories), set `auto_executable: true` so the background executor handles them.

## Tool Usage Guidelines

- Use tools when needed, don't guess about file contents or web information.
- When editing files, use the edit_file tool with exact text matching.
- Always confirm destructive operations before executing them.
- Search memory before storing new facts to avoid duplicates.
- Create tasks with due dates for anything that needs to happen in the future.
"""
```

- [ ] **Step 2: Run tests**

Run: `pytest tests/test_tools.py tests/test_task_executor.py -v`
Expected: All tests PASS

- [ ] **Step 3: Commit**

```bash
git add ares/prompts.py
git commit -m "docs(prompts): update system prompt with auto-execution documentation"
```

---

### Task 8: Add /tasks help text for new commands

**Files:**
- Modify: `ares/cli.py:276-293` (_handle_command /help branch)

- [ ] **Step 1: Update /help output**

In `ares/cli.py`, update the /help table to include new commands. Add these rows after the existing task rows:

```python
            table.add_row("/tasks auto on ID", "Mark task for auto-execution")
            table.add_row("/tasks auto off ID", "Remove task from auto-execution")
            table.add_row("/tasks auto list", "Show auto-executable tasks")
            table.add_row("/tasks history", "Show recently auto-executed tasks")
```

- [ ] **Step 2: Run tests**

Run: `pytest tests/test_cli.py -v`
Expected: All tests PASS

- [ ] **Step 3: Commit**

```bash
git add ares/cli.py
git commit -m "docs(cli): add auto-execution commands to /help"
```

---

### Task 9: Final integration tests

**Files:**
- Modify: `tests/test_task_executor.py`

- [ ] **Step 1: Add end-to-end integration test**

Add to `tests/test_task_executor.py`:

```python
def test_full_workflow_end_to_end(tmp_path):
    """End-to-end: create task -> auto mark -> executor runs -> user checks history."""
    from ares.tools import ToolExecutor
    from ares.memory import MemoryStore

    memory = MemoryStore(db_path=tmp_path / "memory.db")
    store = TaskStore(db_path=tmp_path / "tasks.db")
    tool_executor = ToolExecutor(memory_store=memory, task_store=store)

    # 1. Create an auto-executable task
    result = tool_executor.execute("create_task", {
        "title": "Research Python async patterns",
        "description": "Find best practices for asyncio",
        "auto_executable": True,
    })
    assert "Created task" in result
    assert "[auto]" in result

    # 2. Verify it appears in auto_executable list
    auto_tasks = store.get_auto_executable()
    assert len(auto_tasks) == 1
    assert auto_tasks[0]["title"] == "Research Python async patterns"

    # 3. Run executor
    executed = []

    async def fake_runner(prompt, max_turns):
        executed.append(prompt)
        return {"summary": "Found 3 articles about asyncio patterns."}

    executor = TaskExecutor(
        task_store=store,
        agent_runner=fake_runner,
        enabled=True,
    )
    asyncio.get_event_loop().run_until_complete(executor.run_once())

    # 4. Verify task is now done
    task = store.get(1)
    assert task["status"] == "done"
    assert "Found 3 articles" in task["execution_notes"]
    assert task["executed_at"] is not None

    # 5. Verify history shows it
    history = store.get_recently_executed()
    assert len(history) == 1
    assert history[0]["title"] == "Research Python async patterns"

    # 6. Verify it's no longer in auto_executable list
    auto_tasks = store.get_auto_executable()
    assert len(auto_tasks) == 0

    # 7. Verify get_execution_status tool works
    status_result = tool_executor.execute("get_execution_status", {"limit": 10})
    assert "Research Python async patterns" in status_result
    assert "✅" in status_result

    memory.close()
    store.close()
```

- [ ] **Step 2: Run all tests**

Run: `pytest tests/test_task_executor.py -v`
Expected: All tests PASS

- [ ] **Step 3: Run full test suite**

Run: `pytest tests/ -v --tb=short`
Expected: All tests PASS (no regressions)

- [ ] **Step 4: Commit**

```bash
git add tests/test_task_executor.py
git commit -m "test(executor): add end-to-end integration test for proactive task execution"
```

---

### Task 10: Smoke test the full system

- [ ] **Step 1: Start Ares and create an auto-executable task**

Run: `python -m ares`

In the Ares prompt, type:
```
Create a task to research Python async patterns and mark it for auto-execution
```

Expected: Task created with `[auto]` marker.

- [ ] **Step 2: Verify auto-executable tasks list**

Type: `/tasks auto list`

Expected: Shows the task you just created.

- [ ] **Step 3: Wait for executor to run**

Wait 5 minutes (or temporarily reduce `task_executor_poll_seconds` in config to test faster).

Expected: Auto-completion notification appears in terminal.

- [ ] **Step 4: Check execution history**

Type: `/tasks history`

Expected: Shows the completed task with execution notes.

- [ ] **Step 5: Verify task status**

Type: `/tasks all`

Expected: Task shows status "done" with executed_at timestamp.

---

## Out of Scope (for this plan)

- Recursive task creation (tasks creating more tasks)
- Destructive file operations in auto-execution
- Cross-session persistence of executor state (tasks persist, executor restarts fresh)
- LLM-based task classification (keyword matching is sufficient for v1)
- Cost tracking per execution (future enhancement)
- Priority-based execution ordering (all auto-tasks treated equally in v1)
