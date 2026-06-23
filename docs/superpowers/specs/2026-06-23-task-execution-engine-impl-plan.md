# Task Execution Engine — Phase 1 Implementation Plan

**Date:** 2026-06-23
**Spec:** `2026-06-23-task-execution-engine-design.md`
**Status:** Ready to implement

---

## Research Insights Applied

### From LangGraph — Checkpoint-Per-Step Persistence
LangGraph saves a full checkpoint after every super-step. If a process crashes, it restarts from the last checkpoint — not from scratch. We adopt this: **every completed step is immediately persisted** to SQLite with `completed_steps`, `current_step`, and `task_events`. A crash at step 3 means steps 1 and 2 are fully recoverable.

### From Temporal — Exponential Backoff with Configurable Retry Policy
Temporal's retry policy: `min(Initial × Coefficient^retries, MaximumInterval)` with per-error override. We adopt: exponential backoff (5s → 15s → 45s) with `retry_reason` stored per attempt, matching Temporal's `ApplicationFailure` pattern.

### From OpenAI Assistants — Run/Step Lifecycle
OpenAI's Run object tracks status, steps, tool calls, and token budgets. Each Run Step has its own status mirroring the Run. We adopt: each plan step has its own status (`pending` → `running` → `completed` → `failed`), and the task's `current_step` + `completed_steps` track overall progress.

### From AutoGen MagenticOne — Stall Detection
MagenticOne tracks a `stall_counter` — if progress is not made, it triggers re-planning. We adopt a lightweight version: if a step fails and retries are exhausted, the task moves to `failed` with a clear reason, and the user can `resume_task` to re-plan and retry.

### From CrewAI — Callback-Based Observability
CrewAI fires `step_callback` after every agent step and `task_callback` after each task. We adopt: `task_events` table records every meaningful event (state changes, step starts, tool calls, completions, failures) with timestamps for a human-readable execution log.

---

## Implementation Order

12 steps, ordered by dependency. Each step produces a working, testable increment.

---

### Step 1: Database Schema Migration

**File:** `ares/tools/tasks.py`

**What to do:**
- Add `_migrate_v2()` method to TaskStore that runs on first access
- Add new columns to `tasks` table via `ALTER TABLE ... ADD COLUMN` (idempotent — SQLite errors on duplicate columns are caught and ignored)
- Create `task_events` table
- Create `task_artifacts` table
- Migrate existing data: map `status` → `state`
- Call `_migrate_v2()` at the end of `__init__`

**Schema additions:**

```sql
-- New columns on tasks
ALTER TABLE tasks ADD COLUMN state TEXT DEFAULT 'pending';
ALTER TABLE tasks ADD COLUMN plan TEXT;
ALTER TABLE tasks ADD COLUMN current_step INTEGER DEFAULT 0;
ALTER TABLE tasks ADD COLUMN total_steps INTEGER DEFAULT 0;
ALTER TABLE tasks ADD COLUMN completed_steps TEXT;
ALTER TABLE tasks ADD COLUMN attempt INTEGER DEFAULT 1;
ALTER TABLE tasks ADD COLUMN max_attempts INTEGER DEFAULT 3;
ALTER TABLE tasks ADD COLUMN retry_reason TEXT;
ALTER TABLE tasks ADD COLUMN completion_report TEXT;

-- Migration for existing tasks
UPDATE tasks SET state = 'completed' WHERE status = 'done';
UPDATE tasks SET state = 'failed' WHERE status = 'partial';
UPDATE tasks SET state = 'cancelled' WHERE status = 'cancelled';
UPDATE tasks SET state = 'pending' WHERE status = 'pending';
UPDATE tasks SET state = 'running' WHERE status = 'in_progress';

-- New tables
CREATE TABLE IF NOT EXISTS task_events (...);
CREATE TABLE IF NOT EXISTS task_artifacts (...);
```

**Key decisions:**
- Use `_ensure_column()` pattern already in the codebase (catch `OperationalError` for duplicates)
- Migration runs every startup — idempotent by design (IF NOT EXISTS + catch duplicates)
- No Alembic — keep it simple like the existing codebase

**Tests to write:**
- `test_migrate_v2_adds_new_columns` — verify columns exist after migration
- `test_migrate_v2_preserves_existing_data` — verify old tasks get correct state
- `test_migrate_v2_creates_events_table` — verify table structure
- `test_migrate_v2_creates_artifacts_table` — verify table structure
- `test_migrate_v2_is_idempotent` — call twice, no errors

---

### Step 2: TaskStore New Methods

**File:** `ares/tools/tasks.py`

**What to do:**
Add these methods to TaskStore:

```python
# Event logging
def add_event(self, task_id: int, level: str, step: int | None, message: str) -> int:
    """Insert a task event. Returns event ID."""

def get_events(self, task_id: int, limit: int = 50) -> list[dict]:
    """Get events for a task, oldest first."""

# Artifact tracking
def add_artifact(self, task_id: int, artifact: dict) -> int:
    """Insert a task artifact. Returns artifact ID."""

def get_artifacts(self, task_id: int) -> list[dict]:
    """Get all artifacts for a task."""

# Plan management
def update_plan(self, task_id: int, plan: list[dict], total_steps: int):
    """Store execution plan and reset step tracking."""

def mark_step_completed(self, task_id: int, step_num: int, completed_steps: list[int]):
    """Mark a step as completed and update current_step."""

# State management
def set_state(self, task_id: int, state: str):
    """Update task state and updated_at timestamp."""

def get_tasks_by_state(self, state: str, limit: int = 50) -> list[dict]:
    """Get tasks filtered by state."""
```

**Tests to write:**
- `test_add_event_returns_id`
- `test_get_events_returns_ordered`
- `test_add_artifact_returns_id`
- `test_get_artifacts_returns_all`
- `test_update_plan_stores_json`
- `test_mark_step_completed_updates_tracking`
- `test_set_state_updates_timestamp`
- `test_get_tasks_by_state_filters`

---

### Step 3: TaskPlanner Module

**File:** `ares/planner.py` (new file)

**What to do:**
Create the planner with:
- `generate_plan(task: dict) -> list[dict]`
- JSON parsing with regex fallback (extract ```json...``` blocks)
- Single-step fallback on parse failure
- Planning prompt per spec Section 5.2
- Uses the same LLM client already in the executor

**Design from research:**
- LangGraph doesn't pre-plan — it uses the graph structure. But for Ares, pre-planning is better because tasks are sequential and the user wants visibility into what will happen.
- OpenAI Assistants don't plan either — the model decides steps dynamically. We pre-plan because it enables resume, progress tracking, and completion reports.
- CrewAI's `planning` flag enables pre-execution step planning. Same idea.

**Implementation:**

```python
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
            plan = self._parse_plan(response.get("content", ""))
            return plan
        except Exception as e:
            logger.warning(f"Planning failed, using single-step fallback: {e}")
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

**Tests to write:**
- `test_generate_plan_returns_list`
- `test_parse_plan_json_array`
- `test_parse_plan_markdown_code_block`
- `test_parse_plan_inline_json`
- `test_validate_plan_normalizes`
- `test_fallback_plan_single_step`
- `test_generate_plan_fallback_on_error`
- `test_plan_limits_steps_to_2_8`

---

### Step 4: Models Update

**File:** `ares/models.py`

**What to do:**
Add/update these models:

```python
from enum import Enum

class TaskState(str, Enum):
    QUEUED = "queued"
    PLANNING = "planning"
    RUNNING = "running"
    RETRYING = "retrying"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

    # Legacy mappings
    PENDING = "pending"    # treated as QUEUED
    IN_PROGRESS = "running"
    DONE = "completed"
    PARTIAL = "failed"

# Valid state transitions
TASK_TRANSITIONS = {
    "queued":     ["planning", "cancelled"],
    "planning":   ["running", "failed", "cancelled"],
    "running":    ["completed", "retrying", "failed", "cancelled"],
    "retrying":   ["running", "failed", "cancelled"],
    "completed":  [],
    "failed":     ["queued"],
    "cancelled":  ["queued"],
    "pending":    ["planning", "cancelled"],  # legacy
    "in_progress": ["completed", "retrying", "failed", "cancelled"],
    "done":       [],
    "partial":    ["queued"],
}
```

Keep existing Task and TaskPriority models unchanged for backward compatibility.

**Tests to write:**
- `test_task_state_enum_values`
- `test_task_transitions_valid`
- `test_task_transitions_legacy`

---

### Step 5: TaskExecutor Rewrite

**File:** `ares/task_executor.py`

**What to do:**
This is the core change. Rewrite `_process_task()` to:
1. Enter `planning` state
2. Call `TaskPlanner.generate_plan()`
3. Store plan in DB
4. Execute steps one at a time via `_execute_step()`
5. Persist progress after each step
6. Generate completion report on success
7. Handle failures with retry logic

**Add new methods:**
- `_execute_step(task, step)` — mini agent loop per step
- `_execute_steps_from(task, plan, completed)` — resume-aware step loop
- `_handle_failure(task_id, task, reason)` — retry with exponential backoff
- `_resume_task(task)` — resume from last uncompleted step
- `_generate_completion_report(task, plan)` — LLM summary
- `_track_artifacts(task_id, artifacts, step_num)` — store artifacts
- `_build_artifact_entry(path, step_num)` — enrich with file metadata
- `_log_event(task_id, level, step, message)` — event logging
- `_set_state(task_id, state)` — state transition with logging
- `_format_size(size_bytes)` — human-readable file size

**Key design:**
- Each step gets its own `messages` list (fresh context per step)
- Steps share the same `tool_executor` and `allowed_tools`
- `completed_steps` is persisted after every step completion (LangGraph checkpoint pattern)
- `task_events` is written after every meaningful action (CrewAI callback pattern)
- Retry uses exponential backoff: `min(5 * 3^attempt, 45)` seconds (Temporal pattern)

**Full rewrite of `_process_task`:**

```python
async def _process_task(self, task):
    task_id = task["id"]

    try:
        # Phase 1: Planning
        self._set_state(task_id, "planning")
        self._log_event(task_id, "info", None, "Generating execution plan...")

        plan = await self.planner.generate_plan(task)
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

    except Exception as e:
        logger.error(f"Task {task_id} failed during planning: {e}")
        self._set_state(task_id, "failed")
        self.task_store.update(task_id,
            execution_notes=f"Planning failed: {e}",
            executed_at=now_local_iso(),
        )
        self._log_event(task_id, "error", None, f"Planning failed: {e}")
```

**`_execute_steps_from` — the core step loop:**

```python
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
```

**`_execute_step` — mini agent loop per step:**

```python
async def _execute_step(self, task: dict, step: dict) -> dict:
    """Execute a single plan step via agent loop."""
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

    for turn in range(task.get("max_turns", 10)):
        response = await self.llm.chat(messages, tools=self.allowed_tools)

        if response.get("tool_calls"):
            for call in response["tool_calls"]:
                tool_name = call.get("tool") or call.get("function", {}).get("name", "")
                tool_args = call.get("args") or call.get("function", {}).get("arguments", {})
                result = self.tool_executor.execute(tool_name, tool_args)
                tool_call_count += 1

                # Track file artifacts
                if tool_name in ("write_file", "edit_file", "create_directory"):
                    artifacts.append({
                        "path": tool_args.get("path", ""),
                        "type": tool_name,
                        "timestamp": now_local_iso(),
                    })

                messages.append({"role": "tool", "tool_call_id": call.get("id", ""), "content": result})
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
```

**`_handle_failure` — retry with exponential backoff:**

```python
async def _handle_failure(self, task_id: int, task: dict, reason: str) -> None:
    """Handle a step failure — retry or give up."""
    attempt = task.get("attempt", 1)
    max_attempts = task.get("max_attempts", 3)

    if attempt >= max_attempts:
        self._set_state(task_id, "failed")
        self.task_store.update(task_id,
            execution_notes=f"Failed after {attempt} attempts. Last error: {reason}",
            executed_at=now_local_iso(),
        )
        self._log_event(task_id, "error", None,
            f"Failed after {attempt} attempts: {reason}")
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

    # Re-queue for next poll cycle
    # (executor will pick it up on next run_once)
```

**`_resume_task` — resume from last uncompleted step:**

```python
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
```

**`_generate_completion_report` — LLM summary:**

```python
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

    # Enrich with metadata
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
```

**Helper methods:**

```python
def _log_event(self, task_id: int, level: str, step: int | None, message: str):
    """Insert a task event."""
    self.task_store.add_event(task_id, level=level, step=step, message=message)

def _set_state(self, task_id: int, state: str):
    """Update task state and log the transition."""
    self.task_store.update(task_id, state=state, status=state)  # keep status in sync
    self._log_event(task_id, "info", None, f"State → {state}")

def _track_artifacts(self, task_id: int, artifacts: list[dict], step_num: int):
    """Track files created/modified during a step."""
    for artifact in artifacts:
        entry = self._build_artifact_entry(artifact, step_num)
        self.task_store.add_artifact(task_id, entry)

def _build_artifact_entry(self, artifact: dict, step_num: int) -> dict:
    """Enrich artifact with file metadata."""
    path = artifact.get("path", "")
    try:
        stat = os.stat(path)
        size = stat.st_size
    except OSError:
        size = 0

    line_count = None
    description = None
    if path.endswith(('.md', '.txt', '.py', '.js', '.json', '.yaml')):
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
```

**Tests to write:**
- `test_process_task_planning_phase`
- `test_process_task_step_execution`
- `test_process_task_completion`
- `test_process_task_planning_failure`
- `test_execute_step_tool_calls`
- `test_execute_step_artifacts`
- `test_execute_step_max_turns`
- `test_handle_failure_retry`
- `test_handle_failure_exhausted`
- `test_resume_task_from_step`
- `test_resume_task_all_completed`
- `test_completion_report_generation`
- `test_log_event_creates_record`
- `test_set_state_updates_both_fields`
- `test_track_artifacts_stores_entries`
- `test_build_artifact_entry_enriches`
- `test_format_size_units`

---

### Step 6: Tool Definitions Update

**File:** `ares/tools/definitions.py`

**What to do:**
Add three new tool definitions:

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

**Also update:** `create_task` definition to include new fields:

```python
"auto_executable": {"type": "boolean", "description": "Whether Ares should auto-execute this task in the background (default false)"},
"max_turns": {"type": "integer", "description": "Max agent turns per step during auto-execution (default 10)"},
"max_attempts": {"type": "integer", "description": "Max retry attempts on failure (default 3)"},
```

**Tests to write:**
- `test_resume_task_definition_valid`
- `test_get_task_events_definition_valid`
- `test_get_task_artifacts_definition_valid`
- `test_create_task_definition_includes_max_attempts`

---

### Step 7: ToolExecutor Handlers

**File:** `ares/tools/executor.py`

**What to do:**
Add three new handlers and wire them into `execute()`:

```python
# In __init__:
self.task_executor_ref = None  # set by server

# In execute() handlers dict:
"resume_task": self._resume_task,
"get_task_events": self._get_task_events,
"get_task_artifacts": self._get_task_artifacts,

# Handlers:
def _resume_task(self, args: dict) -> str:
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

**Wire executor reference in server.py:**

```python
# In AresServer.__init__ or run_forever:
if hasattr(self.agent, "tool_executor"):
    self.agent.tool_executor.task_executor_ref = self.task_executor
```

**Tests to write:**
- `test_resume_task_handler_success`
- `test_resume_task_handler_not_found`
- `test_resume_task_handler_wrong_state`
- `test_resume_task_handler_no_plan`
- `test_get_task_events_handler_empty`
- `test_get_task_events_handler_with_events`
- `test_get_task_artifacts_handler_empty`
- `test_get_task_artifacts_handler_with_artifacts`

---

### Step 8: Server Integration

**File:** `ares/server.py`

**What to do:**
1. Update `_execute_task_in_background()` to support planning + step-by-step execution
2. Enhance `task_auto_complete` WebSocket event with completion report
3. Wire `task_executor_ref` to tool executor
4. Add `_resume_task_background()` for resume flow

**Key changes:**

```python
# In _execute_task_in_background:
async def _execute_task_in_background(self, task: dict) -> dict:
    """Execute a task with planning + step-by-step execution."""
    from ares.planner import TaskPlanner

    task_id = task["id"]
    llm = LLMClient(self.config)
    planner = TaskPlanner(llm)

    # Phase 1: Planning
    self.task_store.update(task_id, state="planning")
    plan = await planner.generate_plan(task)
    self.task_store.update(task_id,
        plan=json.dumps(plan),
        total_steps=len(plan),
    )

    # Phase 2: Execute steps
    results = []
    for step in plan:
        step_result = await self._execute_step_background(task, step, llm)
        results.append(step_result)

    # Phase 3: Completion report
    summary = self._build_background_summary(task, plan, results)
    self.task_store.update(task_id,
        state="completed",
        execution_notes=summary,
        executed_at=now_local_iso(),
        completion_report=json.dumps({
            "summary": summary,
            "steps": len(plan),
        }),
    )

    return {"status": "completed", "notes": summary, "plan": plan}
```

**Also update the `_notify_auto_complete` callback:**

```python
def _notify_auto_complete(self, task: dict, result: dict):
    """Notify connected clients of task completion."""
    report = result.get("report", {})
    event = {
        "type": "task_auto_complete",
        "task_id": task["id"],
        "state": task.get("state", "completed"),
        "title": task["title"],
        "report": report,
    }
    # Send to all connected WebSocket clients
    for ws in self._connected_websockets:
        asyncio.ensure_future(self._send(ws, event))
```

**Tests to write:**
- `test_execute_task_in_background_with_plan`
- `test_notify_auto_complete_sends_event`
- `test_server_wires_task_executor_ref`

---

### Step 9: CLI Updates

**File:** `ares/cli.py`

**What to do:**
Add commands:
- `/tasks events <id>` — show execution log for a task
- `/tasks artifacts <id>` — show files created by a task
- `/tasks resume <id>` — resume a failed task
- `/tasks detail <id>` — show full task details including plan, progress, report

**Update existing commands:**
- `/tasks list` — show state badges (colored) instead of just status
- `/tasks info <id>` — show plan progress bar

**Badge colors:**
```
queued      gray
planning    blue
running     cyan
retrying    orange
completed   green
failed      red
cancelled   purple
```

**Progress bar format:**
```
[■■□□] Step 2/4 — Research tokenization
```

**Tests to write:**
- `test_cli_tasks_events_command`
- `test_cli_tasks_artifacts_command`
- `test_cli_tasks_resume_command`
- `test_cli_tasks_detail_shows_plan`
- `test_cli_tasks_list_shows_state_badges`

---

### Step 10: WebSocket Event Enhancement

**File:** `ares/server.py`

**What to do:**
Enhance the status payload to include executor state:

```python
def _status(self) -> dict:
    """Return server status including executor state."""
    base = { ... }  # existing status fields
    base["executor_state"] = self.task_executor.state if self.task_executor else "unknown"
    base["executor_current_task"] = self.task_executor.current_task if self.task_executor else None
    base["executor_tasks_completed"] = self.task_executor.stats.get("tasks_completed", 0)
    base["executor_tasks_failed"] = self.task_executor.stats.get("tasks_failed", 0)
    return base
```

Add new WebSocket message types:

```python
# Client sends:
{"type": "task:resume", "task_id": 13}
# Server responds:
{"type": "task:resumed", "task_id": 13, "message": "Task #13 queued for resume"}

# Client sends:
{"type": "task:events", "task_id": 13}
# Server responds:
{"type": "task:events", "task_id": 13, "events": [...]}

# Client sends:
{"type": "task:artifacts", "task_id": 13}
# Server responds:
{"type": "task:artifacts", "task_id": 13, "artifacts": [...]}
```

**Tests to write:**
- `test_websocket_task_resume`
- `test_websocket_task_events`
- `test_websocket_task_artifacts`

---

### Step 11: Backward Compatibility Audit

**What to do:**
Audit all code that reads/writes `status` field and ensure it works with the new `state` field:

1. `create_task` tool — creates with `state='queued'` and `status='pending'`
2. `complete_task` tool — sets `state='completed'` and `status='done'`
3. `cancel_task` tool — sets `state='cancelled'` and `status='cancelled'`
4. `list_tasks` — reads `state` for display, falls back to `status`
5. `search_tasks` — same fallback
6. `get_due_soon` — filters on `state IN ('pending', 'queued')`
7. `get_auto_executable` — filters on `state IN ('pending', 'queued')` + `auto_executable='yes'`
8. `get_recently_executed` — filters on `executed_at IS NOT NULL`
9. Server `_status()` — uses `state` for executor stats
10. CLI display — uses `state` for badges

**Tests to write:**
- `test_create_task_sets_both_fields`
- `test_complete_task_sets_both_fields`
- `test_cancel_task_sets_both_fields`
- `test_list_tasks_reads_state`
- `test_get_auto_executable_filters_by_state`

---

### Step 12: Integration Tests

**File:** `tests/test_task_execution_engine.py` (new file)

**What to do:**
Write end-to-end integration tests:

```python
# Full lifecycle test
async def test_full_task_lifecycle():
    """create → plan → execute → complete"""

# Resume test
async def test_resume_failed_task():
    """create → plan → execute (fail at step 2) → resume → complete"""

# Retry test
async def test_retry_on_failure():
    """create → execute (fail) → retry → succeed"""

# Migration test
async def test_migration_preserves_existing_tasks():
    """existing tasks get correct state mapping"""

# Completion report test
async def test_completion_report_generated():
    """completed task has a completion report"""

# Event logging test
async def test_events_recorded_throughout_lifecycle():
    """every state change and step has events"""

# Artifact tracking test
async def test_artifacts_tracked_during_execution():
    """files created during execution are tracked"""

# Backward compatibility test
async def test_old_api_still_works():
    """status field still works for old consumers"""
```

---

## File Change Summary

| File | Action | Lines Changed (est.) |
|------|--------|---------------------|
| `ares/tools/tasks.py` | Modify | +150 (migration + new methods) |
| `ares/planner.py` | **Create** | +150 |
| `ares/models.py` | Modify | +40 (TaskState enum + transitions) |
| `ares/task_executor.py` | **Rewrite** | ~400 (core engine) |
| `ares/tools/definitions.py` | Modify | +30 (3 new tools) |
| `ares/tools/executor.py` | Modify | +80 (3 new handlers) |
| `ares/server.py` | Modify | +60 (integration) |
| `ares/cli.py` | Modify | +50 (new commands) |
| `tests/test_tasks.py` | Modify | +40 (migration tests) |
| `tests/test_task_executor.py` | Modify | +200 (rewrite tests) |
| `tests/test_task_execution_engine.py` | **Create** | +300 |

**Total: ~1,500 lines changed/added**

---

## Implementation Order Dependencies

```
Step 1 (DB Schema)     ← no dependencies
Step 2 (Store Methods) ← depends on Step 1
Step 3 (Planner)       ← no dependencies (can parallel with 1-2)
Step 4 (Models)        ← no dependencies (can parallel with 1-3)
Step 5 (Executor)      ← depends on Steps 1, 2, 3, 4
Step 6 (Tool Defs)     ← depends on Step 4
Step 7 (Tool Handlers) ← depends on Steps 2, 6
Step 8 (Server)        ← depends on Steps 3, 5, 7
Step 9 (CLI)           ← depends on Steps 2, 7
Step 10 (WebSocket)    ← depends on Steps 5, 8
Step 11 (Backward Compat) ← depends on all above
Step 12 (Integration Tests) ← depends on all above
```

**Parallel-safe group:**
- Steps 1, 3, 4 can be done in parallel
- Steps 6, 9 can be done in parallel
- Steps 10, 11 can be done in parallel

**Critical path:** 1 → 2 → 5 → 8 → 12
