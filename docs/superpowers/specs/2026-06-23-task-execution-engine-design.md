# Task Execution Engine — Phase 1 Design

**Date:** 2026-06-23
**Status:** Approved
**Scope:** Transform Ares task system from simple pending/done to a full execution engine with planning, step tracking, resume, retry metadata, execution logs, artifact tracking, and completion reports.

---

## 1. Goal

Transform the Ares task execution system from:

```
pending → running → completed
```

Into:

```
queued → planning → running → [retrying] → completed
                                        ↘ failed
                                        ↘ cancelled
```

While keeping the existing architecture (SQLite, TaskExecutor, WebSocket server) intact.

---

## 2. Current State Summary

### Database

- Table: `tasks` with columns: id, title, description, due, priority, status, created_at, updated_at, completed_at, reminder_at, reminder_sent_at, original_due_text, auto_executable, execution_notes, executed_at, max_turns, retry_count

### States (current)

- `pending`, `in_progress`, `done`, `partial`, `cancelled`

### Executor

- `TaskExecutor` in `ares/task_executor.py`
- Polls every 300s for auto-executable tasks
- Classifies tasks by keywords (research, file, memory)
- Runs isolated agent loop with max_turns limit
- Retry: increments retry_count, re-queues up to 3 times

### Key Files

| File | Role |
|------|------|
| `ares/tools/tasks.py` | TaskStore — all DB CRUD and queries |
| `ares/task_executor.py` | TaskExecutor — background execution engine |
| `ares/tools/executor.py` | ToolExecutor — dispatches tool calls |
| `ares/tools/definitions.py` | Tool schemas for OpenAI function-calling |
| `ares/server.py` | WebSocket server, executor integration |
| `ares/cli.py` | CLI with task commands |
| `ares/models.py` | Pydantic models |

---

## 3. Database Schema Changes

### 3.1 New Columns on `tasks` Table

```sql
ALTER TABLE tasks ADD COLUMN state TEXT DEFAULT 'pending';
ALTER TABLE tasks ADD COLUMN plan TEXT;              -- JSON array of steps
ALTER TABLE tasks ADD COLUMN current_step INTEGER DEFAULT 0;
ALTER TABLE tasks ADD COLUMN total_steps INTEGER DEFAULT 0;
ALTER TABLE tasks ADD COLUMN completed_steps TEXT;    -- JSON array of step indices
ALTER TABLE tasks ADD COLUMN attempt INTEGER DEFAULT 1;
ALTER TABLE tasks ADD COLUMN max_attempts INTEGER DEFAULT 3;
ALTER TABLE tasks ADD COLUMN retry_reason TEXT;
ALTER TABLE tasks ADD COLUMN completion_report TEXT;  -- JSON report
```

### 3.2 Migration for Existing Tasks

```sql
UPDATE tasks SET state = 'completed' WHERE status = 'done';
UPDATE tasks SET state = 'failed' WHERE status = 'partial';
UPDATE tasks SET state = 'cancelled' WHERE status = 'cancelled';
UPDATE tasks SET state = 'pending' WHERE status = 'pending';
UPDATE tasks SET state = 'running' WHERE status = 'in_progress';
```

The `status` column stays for backward compatibility but `state` becomes the primary field.

### 3.3 New Table: `task_events`

```sql
CREATE TABLE IF NOT EXISTS task_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id    INTEGER NOT NULL,
    timestamp  TEXT DEFAULT (datetime('now')),
    level      TEXT DEFAULT 'info',  -- info, success, warning, error
    step       INTEGER,             -- which step (null for task-level)
    message    TEXT NOT NULL,
    FOREIGN KEY (task_id) REFERENCES tasks(id)
);
```

### 3.4 New Table: `task_artifacts`

```sql
CREATE TABLE IF NOT EXISTS task_artifacts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id       INTEGER NOT NULL,
    step          INTEGER,
    path          TEXT NOT NULL,
    artifact_type TEXT NOT NULL,    -- write_file | edit_file | create_directory
    size_bytes    INTEGER DEFAULT 0,
    size_human    TEXT DEFAULT '0 B',
    line_count    INTEGER,
    description   TEXT,
    created_at    TEXT DEFAULT (datetime('now')),
    FOREIGN KEY (task_id) REFERENCES tasks(id)
);
```

---

## 4. Task States & Transitions

### 4.1 State Definitions

| State | Meaning | Who sets it |
|-------|---------|-------------|
| `queued` | Task created, waiting for executor | `create_task` tool |
| `planning` | LLM is generating execution plan | Executor |
| `running` | Executor is running steps | Executor |
| `retrying` | Failed, waiting to retry | Executor on failure |
| `completed` | All steps done successfully | Executor after final step |
| `failed` | Max retries exhausted | Executor |
| `cancelled` | User cancelled | `cancel_task` tool |

### 4.2 Allowed Transitions

```python
TRANSITIONS = {
    "queued":     ["planning", "cancelled"],
    "planning":   ["running", "failed", "cancelled"],
    "running":    ["completed", "retrying", "failed", "cancelled"],
    "retrying":   ["running", "failed", "cancelled"],
    "completed":  [],  # terminal
    "failed":     ["queued"],  # manual re-queue via resume
    "cancelled":  ["queued"],  # manual re-queue
}
```

### 4.3 Backward Compatibility

Existing `status` values map to `state`:
- `pending` → `pending` (executor treats as `queued`)
- `in_progress` → `running`
- `done` → `completed`
- `partial` → `failed`
- `cancelled` → `cancelled`

---

## 5. Task Planner

### 5.1 Module: `ares/planner.py`

Single responsibility: take a task title + description, return a list of execution steps.

```python
class TaskPlanner:
    def __init__(self, llm_client, model=None):
        self.llm = llm_client
        self.model = model  # defaults to current session model

    async def generate_plan(self, task: dict) -> list[dict]:
        """Generate an execution plan for a task.

        Returns:
            [
                {"step": 1, "title": "...", "description": "...", "status": "pending"},
                {"step": 2, "title": "...", "description": "...", "status": "pending"},
            ]
        """
```

### 5.2 Planning Prompt

```
You are a task planner. Break the following task into clear, actionable steps.

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
- Return ONLY the JSON array, no other text
```

### 5.3 Output Parsing

- Planner calls `llm.chat()` (not streaming)
- Extracts JSON array from response
- Validates each step has required fields
- On parse failure: fall back to single-step plan

### 5.4 Integration Point

```
Task Created (queued)
    ↓
Executor picks up task
    ↓
PLANNING: planner.generate_plan(task)
    ↓
Store plan in tasks.plan (JSON)
    ↓
RUNNING: execute step-by-step
```

---

## 6. Step-by-Step Executor

### 6.1 Modified `_process_task()`

```python
async def _process_task(self, task):
    task_id = task["id"]

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

    # Phase 2: Execute steps
    self._set_state(task_id, "running")
    self._log_event(task_id, "success", None, f"Plan ready: {len(plan)} steps")

    for step in plan:
        step_num = step["step"]

        self.task_store.update(task_id, current_step=step_num)
        self._log_event(task_id, "info", step_num, f"Starting: {step['title']}")

        try:
            result = await self._execute_step(task, step)
            self._log_event(task_id, "success", step_num, f"Completed: {step['title']}")

            completed = json.loads(task.get("completed_steps") or "[]")
            completed.append(step_num)
            self.task_store.update(task_id, completed_steps=json.dumps(completed))

            if result.get("artifacts"):
                self._track_artifacts(task_id, result["artifacts"], step_num)

        except Exception as e:
            self._log_event(task_id, "error", step_num, f"Failed: {step['title']}: {e}")
            return await self._handle_failure(task_id, task, str(e))

    # Phase 3: Generate completion report
    self._log_event(task_id, "info", None, "Generating completion report...")
    report = await self._generate_completion_report(task, plan)
    self.task_store.update(task_id,
        state="completed",
        completion_report=json.dumps(report),
        executed_at=now_local_iso(),
    )
    self._log_event(task_id, "success", None, "Task completed successfully")
```

### 6.2 Step Execution (`_execute_step`)

Each step runs its own mini agent loop with a focused prompt:

```python
async def _execute_step(self, task: dict, step: dict) -> dict:
    """Execute a single plan step via agent loop.

    Returns:
        {"status": "success"|"error", "output": str, "artifacts": list}
    """
    prompt = (
        f"Task: {task['title']}\n"
        f"Step {step['step']}/{task['total_steps']}: {step['title']}\n"
        f"Instructions: {step['description']}\n\n"
        f"Execute this step using available tools. "
        f"When done, provide a brief summary."
    )

    messages = [self._build_system_prompt(), {"role": "user", "content": prompt}]
    artifacts = []

    for turn in range(task.get("max_turns", 10)):
        response = await self.llm.chat(messages, tools=self.allowed_tools)

        if response.get("tool_calls"):
            for call in response["tool_calls"]:
                result = self.tool_executor.execute(call["tool"], call["args"])

                if call["tool"] in ("write_file", "edit_file", "create_directory"):
                    artifacts.append({
                        "path": call["args"].get("path", ""),
                        "type": call["tool"],
                        "timestamp": now_local_iso(),
                    })

                messages.append({"role": "tool", "tool_call_id": call["id"], "content": result})
            messages.append({"role": "assistant", "content": None, "tool_calls": response["tool_calls"]})
        else:
            return {"status": "success", "output": response.get("content", ""), "artifacts": artifacts}

    return {"status": "success", "output": "Step completed (max turns reached)", "artifacts": artifacts}
```

---

## 7. Execution Log & Events

### 7.1 `TaskEvent` Schema

```python
@dataclass
class TaskEvent:
    id: int
    task_id: int
    timestamp: str       # ISO format
    level: str           # info | success | warning | error
    step: int | None     # step number, null for task-level events
    message: str
```

### 7.2 What Gets Logged

| Event | Level | Step | Example |
|-------|-------|------|---------|
| State change | info | null | "Task queued for execution" |
| Plan generated | info | null | "Plan ready: 4 steps" |
| Step starting | info | step# | "Starting: Research transformer architecture" |
| Tool call | info | step# | "Searching web: LLM transformer architecture" |
| Step completed | success | step# | "Completed: Research transformer architecture" |
| Step failed | error | step# | "Failed: Web request timeout" |
| Retry scheduled | warning | null | "Retrying (attempt 2/3): Web request timeout" |
| Task completed | success | null | "Task completed successfully" |
| Task failed | error | null | "Failed after 3 retries" |

### 7.3 Logging Methods

```python
def _log_event(self, task_id: int, level: str, step: int | None, message: str):
    """Insert a task event into the events table."""
    self.task_store.add_event(task_id, level=level, step=step, message=message)

def _set_state(self, task_id: int, state: str):
    """Update task state and log the transition."""
    self.task_store.update(task_id, state=state)
    self._log_event(task_id, "info", None, f"State → {state}")
```

### 7.4 Query Method

```python
def get_events(self, task_id: int, limit: int = 50) -> list[dict]:
    """Get events for a task, oldest first for display."""
    return list(self.db.execute_returning(
        "SELECT * FROM task_events WHERE task_id = ? ORDER BY id ASC LIMIT ?",
        [task_id, limit],
    ))
```

---

## 8. Completion Reports

### 8.1 Report Structure

```python
@dataclass
class CompletionReport:
    title: str                          # Auto-generated action title
    status_emoji: str                   # ✓, ⚠, ✗
    status_label: str                   # "Completed", "Completed with warnings", "Failed"
    summary: str                        # LLM-generated 2-3 sentence summary
    key_results: list[str]              # 3-5 bullet points
    files_created: list[ArtifactEntry]  # with path, size, line count
    files_modified: list[ArtifactEntry]
    steps_completed: int
    total_steps: int
    duration_human: str                 # "1m 32s" or "3h 12m"
    duration_seconds: float
    tool_calls_made: int
    attempt: int
    max_attempts: int
    started_at: str
    completed_at: str
    model_used: str

@dataclass
class ArtifactEntry:
    path: str
    size_bytes: int
    size_human: str          # "13.5 KB"
    line_count: int | None
    description: str | None  # LLM-generated one-liner per file
```

### 8.2 Artifact Enrichment

```python
def _build_artifact_entry(self, artifact: dict) -> dict:
    """Enrich artifact with file metadata."""
    path = artifact["path"]
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
        "path": path,
        "size_bytes": size,
        "size_human": _format_size(size),
        "line_count": line_count,
        "description": description,
    }
```

### 8.3 Report Generation Prompt

```python
prompt = (
    "Generate a polished completion report for this task.\n\n"
    f"Task: {task['title']}\n"
    f"Description: {task.get('description', 'N/A')}\n\n"
    f"Steps executed:\n"
    + "\n".join(f"  {s['step']}. {s['title']}" for s in plan) + "\n\n"
    f"Files created: {[a['path'] for a in artifacts if a['type'] == 'write_file']}\n"
    f"Files modified: {[a['path'] for a in artifacts if a['type'] == 'edit_file']}\n\n"
    "Return a JSON object:\n"
    '{\n'
    '  "title": "action verb + topic (max 60 chars)",\n'
    '  "summary": "2-3 sentence executive summary",\n'
    '  "key_results": ["bullet 1", "bullet 2", ...],\n'
    '  "file_descriptions": {"path": "one-line description of what this file contains"}\n'
    '}\n\n'
    "Write in a confident, precise tone. No hedging. Return ONLY the JSON."
)
```

### 8.4 WebSocket Event

```python
{
    "type": "task_auto_complete",
    "task_id": 13,
    "state": "completed",
    "report": { ... }  # full CompletionReport as JSON
}
```

---

## 9. Resume Failed Tasks

### 9.1 Resume Flow

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

**`_execute_steps_from`** is the same step loop from `_process_task()` (Section 6.1) but starting from the first uncompleted step. It takes the full plan, filters out completed steps, and runs the remaining steps in order:

```python
async def _execute_steps_from(self, task: dict, plan: list[dict], completed: list[int]):
    """Execute remaining steps from a plan, skipping already-completed ones."""
    self._set_state(task["id"], "running")

    for step in plan:
        step_num = step["step"]
        if step_num in completed:
            continue

        self.task_store.update(task["id"], current_step=step_num)
        self._log_event(task["id"], "info", step_num, f"Starting: {step['title']}")

        try:
            result = await self._execute_step(task, step)
            self._log_event(task["id"], "success", step_num, f"Completed: {step['title']}")

            completed.append(step_num)
            self.task_store.update(task["id"], completed_steps=json.dumps(completed))

            if result.get("artifacts"):
                self._track_artifacts(task["id"], result["artifacts"], step_num)

        except Exception as e:
            self._log_event(task["id"], "error", step_num, f"Failed: {step['title']}: {e}")
            return await self._handle_failure(task["id"], task, str(e))

    # All steps done — generate completion report
    report = await self._generate_completion_report(task, plan)
    self.task_store.update(task["id"],
        state="completed",
        completion_report=json.dumps(report),
        executed_at=now_local_iso(),
    )
    self._log_event(task["id"], "success", None, "Task completed successfully")
```

### 9.2 Resume Tool

```python
_tool(
    "resume_task",
    "Resume a failed task from where it left off. Only works on tasks with state='failed'.",
    {
        "task_id": {"type": "integer", "description": "ID of the failed task to resume"},
    },
    required=["task_id"],
),
```

### 9.3 State Persistence Per Step

Every completed step is persisted immediately, so a crash at step 3 means steps 1 and 2 are fully saved:

```python
# After each step completes:
self.task_store.update(task_id,
    completed_steps=json.dumps(completed),  # [1, 2]
    current_step=next_step["step"],          # 3
    artifacts=json.dumps(all_artifacts),     # files from steps 1-2
)
```

---

## 10. Retry Metadata

### 10.1 Fields

| Field | Type | Default | Purpose |
|-------|------|---------|---------|
| `attempt` | INTEGER | 1 | Current attempt number |
| `max_attempts` | INTEGER | 3 | Maximum allowed attempts |
| `retry_reason` | TEXT | null | Why the last attempt failed |

### 10.2 Retry Flow

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

    # Exponential backoff: 5s, 15s, 45s
    delay = min(5 * (3 ** (attempt - 1)), 45)
    await asyncio.sleep(delay)

    self.task_store.update(task_id, state="queued", attempt=attempt + 1)
```

---

## 11. Artifact Tracking

### 11.1 Tracking During Execution

In `_execute_step`, after each tool call:

```python
if call["tool"] in ("write_file", "edit_file", "create_directory"):
    path = call["args"].get("path", "")
    entry = self._build_artifact_entry(path, step_num)
    self.task_store.add_artifact(task_id, entry)
```

### 11.2 Query Method

```python
def get_artifacts(self, task_id: int) -> list[dict]:
    """Get all artifacts for a task."""
    return list(self.db.execute_returning(
        "SELECT * FROM task_artifacts WHERE task_id = ? ORDER BY id ASC",
        [task_id],
    ))
```

---

## 12. Files to Create/Modify

### New Files

| File | Purpose |
|------|---------|
| `ares/planner.py` | TaskPlanner class |
| `ares/models.py` | Updated with new states and dataclasses |

### Modified Files

| File | Changes |
|------|---------|
| `ares/tools/tasks.py` | New columns, migration, new tables, add_event, add_artifact, get_events, get_artifacts |
| `ares/task_executor.py` | Planning phase, step-by-step execution, resume, retry metadata, event logging, artifact tracking, completion reports |
| `ares/tools/executor.py` | Add resume_task handler, get_task_events handler, get_task_artifacts handler |
| `ares/tools/definitions.py` | Add resume_task, get_task_events, get_task_artifacts tool definitions |
| `ares/server.py` | Update _execute_task_in_background to support planning + steps, enhance task_auto_complete event |
| `ares/cli.py` | Add task events display, artifact display, resume command |
| `ares/models.py` | Add TaskState enum, TaskEvent, ArtifactEntry, CompletionReport |

### Unchanged

| File | Reason |
|------|--------|
| `ares/llm.py` | No changes needed |
| `ares/memory.py` | No changes needed |
| `ares/conversations.py` | No changes needed |
| `ares/agent.py` | No changes needed |

---

## 13. Backward Compatibility

- `status` column stays but `state` becomes primary
- Existing tasks get state mapped via migration SQL
- `create_task` tool still works (creates with state='queued')
- `complete_task` tool updates both status and state
- `cancel_task` tool updates both status and state
- Old UI code reading `status` still works
- New UI code reads `state`

---

## 14. Testing Strategy

1. Unit tests for TaskPlanner (mock LLM, verify JSON parsing, fallback)
2. Unit tests for TaskStore new methods (add_event, get_events, add_artifact, get_artifacts)
3. Unit tests for state transitions
4. Integration test for full task lifecycle: create → plan → execute → complete
5. Integration test for resume: create → plan → execute (fail at step 3) → resume → complete
6. Integration test for retry: create → execute (fail) → retry → succeed
7. Migration test: verify existing tasks get correct state mapping
8. Test completion report generation with mocked LLM

---

## 15. Success Criteria

1. Task goes through all states: queued → planning → running → completed
2. Plan is stored and steps are tracked individually
3. Execution log shows human-readable progress
4. Completion report is polished with file stats and key results
5. Failed tasks can be resumed from last uncompleted step
6. Retry metadata is visible to user
7. Artifacts are tracked with file metadata
8. All existing tests pass
9. Backward compatible with existing tasks in database
