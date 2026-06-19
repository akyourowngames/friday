# Proactive Task Execution — Design Spec

**Date:** 2026-06-19
**Status:** Draft
**Author:** Claude (brainstorming session)

---

## Overview

Add a proactive task execution layer to Ares. A background polling loop scans for pending tasks marked as auto-executable, evaluates whether Ares can complete them with its existing tools, and executes them autonomously. Users are notified instantly when tasks are completed or partially completed.

## Current State

Ares has a basic task system:
- **Model:** `Task` with id, title, description, due, priority, status (pending/done/cancelled), reminder_at
- **Operations:** create, list, complete, cancel, search, get_due_soon
- **Reminders:** Background loop polls every 30s, sends desktop notifications for tasks with `reminder_at`
- **Context:** Top 5 pending tasks injected into agent prompt on each message

## Design Goals

1. **Proactive execution:** Ares scans and completes tasks autonomously in the background
2. **Instant notification:** User sees results immediately when tasks complete
3. **Safety:** Budget caps, turn limits, restricted tool access for auto-execution
4. **Partial completion:** When Ares can't fully complete a task, it logs what it did and what remains
5. **Minimal model changes:** Extend existing Task model with 4 fields, no new tables

---

## Task Model Changes

### New Fields

| Field | Type | Default | Purpose |
|-------|------|---------|---------|
| `auto_executable` | TEXT | `'no'` | `'yes'` / `'no'` — user opts tasks into auto-completion |
| `execution_notes` | TEXT | `NULL` | What Ares did, what remains |
| `executed_at` | TEXT | `NULL` | When Ares auto-completed/partially completed |
| `max_turns` | INTEGER | `10` | Max tool-use turns for this task's execution |
| `retry_count` | INTEGER | `0` | Number of times execution has been retried after failure |

### New Status Value

| Status | Meaning |
|--------|---------|
| `pending` | Not started (existing) |
| `in_progress` | Being executed by background loop (new) |
| `done` | Completed (existing) |
| `partial` | Attempted but not fully completed (new) |
| `cancelled` | Cancelled by user (existing) |

### Migration

```sql
ALTER TABLE tasks ADD COLUMN auto_executable TEXT DEFAULT 'no';
ALTER TABLE tasks ADD COLUMN execution_notes TEXT;
ALTER TABLE tasks ADD COLUMN executed_at TEXT;
ALTER TABLE tasks ADD COLUMN max_turns INTEGER DEFAULT 10;
```

---

## Background Execution Engine

### Architecture

New module: `ares/task_executor.py`

```
┌─────────────────────────────────────────────────────┐
│                   TaskExecutor                      │
│                                                     │
│  ┌─────────────┐    ┌─────────────┐    ┌─────────┐ │
│  │   Sleep     │───▶│   Scan      │───▶│ Execute │ │
│  │ (5 min)     │    │ (filter)    │    │ (agent) │ │
│  └─────────────┘    └─────────────┘    └─────────┘ │
│                          │                   │      │
│                          ▼                   ▼      │
│                    ┌─────────────┐    ┌─────────┐   │
│                    │  Evaluate   │    │ Notify  │   │
│                    │ (can do?)   │    │ (callback)│  │
│                    └─────────────┘    └─────────┘   │
└─────────────────────────────────────────────────────┘
```

### Execution Cycle

```
1. Sleep (configurable, default 5 min)
2. Scan pending tasks where auto_executable='yes'
3. For each task:
   a. Check status != 'in_progress' (avoid duplicate execution)
   b. Evaluate: can Ares complete this with its tools?
   c. If actionable: execute with turn/cost limits
   d. Notify user of result
4. Cooldown (60s between tasks)
5. Repeat
```

### Task Classification ("Can I do this?")

Lightweight keyword-based evaluation — no LLM calls for the decision itself.

| Category | Keywords | Tools Available |
|----------|----------|-----------------|
| Research | "research", "find out", "look up", "search for", "what is", "how to" | `web_search`, `fetch_url` |
| File/Code | "create file", "write", "find file", "list files", "check" | `read_file`, `search_files`, `glob_pattern`, `get_file_info` |
| Memory | "remind me", "what did I say", "recall", "remember" | `search_memory`, `store_memory` |

If no keywords match → mark as `'partial'` with notes: "Task type not recognized for auto-execution."

### Task Execution (Agent Loop)

For each actionable task, Ares runs an isolated execution:

```
1. Set task status → 'in_progress'
2. Build prompt: "Complete this task: {title}. {description}."
3. Run agent loop with:
   - max_turns: task.max_turns or config default (10)
   - restricted tools: only read-only + memory tools
   - no conversation history (fresh context)
4. On completion:
   - status → 'done'
   - execution_notes → summary of what was done
   - executed_at → timestamp
5. On partial completion:
   - status → 'partial'
   - execution_notes → what was done + what remains
6. On failure:
   - status → 'pending' (retry next cycle)
   - execution_notes → error message
7. Notify user instantly
```

### Tool Restrictions for Auto-Execution

The background executor uses a restricted tool set:

| Tool | Allowed | Reason |
|------|---------|--------|
| `web_search` | ✅ | Research tasks |
| `fetch_url` | ✅ | Research tasks |
| `read_file` | ✅ | File tasks |
| `search_files` | ✅ | File tasks |
| `list_directory` | ✅ | File tasks |
| `glob_pattern` | ✅ | File tasks |
| `get_file_info` | ✅ | File tasks |
| `store_memory` | ✅ | Memory compilation |
| `search_memory` | ✅ | Memory tasks |
| `write_file` | ❌ | Destructive — requires user |
| `edit_file` | ❌ | Destructive — requires user |
| `delete_file` | ❌ | Destructive — requires user |
| `move_file` | ❌ | Destructive — requires user |
| `create_directory` | ❌ | Destructive — requires user |
| `create_task` | ❌ | Avoid recursive task creation |

---

## Safety & Limits

### Hard Guardrails

| Limit | Default | Configurable | Purpose |
|-------|---------|--------------|---------|
| Max turns per task | 10 | Per-task via `max_turns` field | Prevent runaway execution |
| Cooldown between tasks | 60s | No | Prevent API rate limiting |
| Poll interval | 300s (5 min) | Yes, via `task_executor_poll_seconds` | How often to scan |
| Retry limit | 3 failures | No | Stop retrying after 3 failures |

### Error Handling

| Scenario | Handling |
|----------|----------|
| API error / timeout | Retry up to 3 times, then mark partial |
| Task too complex | Mark partial with notes explaining what's needed |
| Turn limit hit | Mark partial, preserve work done |
| Task description too vague | Mark partial: "Task unclear, couldn't determine action" |
| Executor loop error | Log error, continue loop (don't crash) |

### SQLite Concurrency

Enable WAL mode on the database to prevent lock contention between background executor and foreground CLI:

```python
conn.execute("PRAGMA journal_mode = WAL;")
conn.execute("PRAGMA busy_timeout = 5000;")
```

---

## Notification System

### Instant Notification

When Ares auto-completes a task (fully or partially), the user sees:

**Terminal (CLI):**
```
⚡ Auto-completed: "Research Python async patterns"
   ✅ Fully completed
   Notes: Found 3 relevant articles about asyncio, Trio, and concurrent.futures.

⚡ Auto-completed: "Fix the failing tests"
   ⚠️ Partially completed
   Notes: Found 3 failing tests. Fixed 2. The third (test_auth_timeout)
   requires mocking a Redis connection — manual intervention needed.
```

Renders as a `rich.Panel` with yellow border (same as reminder notifications).

**WebSocket Server:**
```json
{
  "type": "task_auto_complete",
  "task_id": 42,
  "title": "Research Python async patterns",
  "status": "done",
  "notes": "Found 3 relevant articles..."
}
```

---

## CLI Commands

| Command | Action |
|---------|--------|
| `/tasks auto on <id>` | Mark task as auto-executable |
| `/tasks auto off <id>` | Unmark task |
| `/tasks auto list` | Show all auto-executable tasks |
| `/tasks history` | Show recently completed/partial tasks with notes |

---

## Agent Tool Changes

### Updated: `create_task`

Add optional parameter:
- `auto_executable` (boolean, default false) — mark task for auto-completion

### New: `get_execution_status`

Show which tasks were auto-completed, with execution notes.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `limit` | integer | `10` | Number of recent executions to show |

Returns: list of tasks with `executed_at` and `execution_notes`.

---

## Configuration

New fields in `AppConfig`:

| Setting | Default | Purpose |
|---------|---------|---------|
| `task_executor_enabled` | `True` | Enable/disable background execution |
| `task_executor_poll_seconds` | `300` | How often to scan (5 min) |
| `task_executor_max_turns` | `10` | Default max turns per task |
| `task_executor_max_cost_usd` | `0.10` | Budget cap per task execution (future) |

---

## File Structure

| File | Action | Responsibility |
|------|--------|----------------|
| `ares/tasks.py` | Modify | Add 4 new fields + migration + `update()` method |
| `ares/task_executor.py` | Create | Background execution engine |
| `ares/tools.py` | Modify | Add `get_execution_status`, update `create_task` |
| `ares/config.py` | Modify | Add 4 new AppConfig fields |
| `ares/cli.py` | Modify | Add `/tasks auto` commands + auto-complete notifications |
| `ares/server.py` | Modify | Add `task_auto_complete` WebSocket event |
| `ares/sqlite_utils.py` | Modify | Enable WAL mode |
| `tests/test_task_executor.py` | Create | Tests for background execution |

---

## End-to-End Flow

```
User: "Research Python async patterns and remind me tomorrow"
  ↓
Agent creates task:
  - title: "Research Python async patterns"
  - due: "2026-06-20"
  - auto_executable: "yes"
  ↓
5 minutes later... background loop wakes up
  ↓
Evaluator: "Research" keyword detected + auto_executable=yes → actionable
  ↓
Executor (isolated context):
  - web_search("Python async patterns")
  - fetch_url(top result)
  - Summarize findings
  ↓
Result:
  - status → 'done'
  - execution_notes → "Found 3 articles about asyncio, Trio, concurrent.futures"
  ↓
⚡ Notification: "Auto-completed: Research Python async patterns"
  ↓
User returns, sees completed task with notes in /tasks history
```

---

## Out of Scope

- Recursive task creation (tasks creating more tasks)
- Destructive file operations in auto-execution
- Cross-session persistence of executor state (tasks persist, executor restarts fresh)
- LLM-based task classification (keyword matching is sufficient for v1)
- Cost tracking per execution (future enhancement)
- Priority-based execution ordering (all auto-tasks treated equally in v1)
