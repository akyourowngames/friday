# Task Executor LLM Optimization — Design Spec

> **For agentic workers:** This is a design spec, not an implementation plan. After approval, invoke `superpowers:writing-plans` to create the implementation plan.

**Goal:** Eliminate 429 rate-limit crashes and reduce LLM calls for auto-executed tasks from 5-15 down to 0-3.

**Architecture:** Three-layer optimization: (1) exponential backoff on 429 errors everywhere, (2) template-based plan generation for common tasks (zero LLM calls), (3) local completion reports for template-matched tasks.

**Tech Stack:** Python stdlib only. No new dependencies.

---

## 1. Problem

| Issue | Impact |
|-------|--------|
| **No 429 retry** | Single rate-limit error kills the entire task immediately |
| **LLM planning per task** | Every task calls the LLM to generate a plan — even simple "write file X" tasks |
| **LLM completion report per task** | Another LLM call after every task to generate a summary |
| **Heavy LLM usage** | A 3-step task uses 5-15 LLM calls — too many for rate-limited providers |

---

## 2. Solution Overview

### 2.1 Retry/Backoff (`ares/llm.py`)

Add exponential backoff to `chat()` and `chat_stream()`:

- On 429: sleep 2s → 4s → 8s, retry up to 3 times
- On other errors: raise immediately
- Log each retry attempt

### 2.2 Template Engine (`ares/task_templates.py`)

New module with `TaskTemplateEngine` class:

- Matches task title/description against keyword patterns
- Returns pre-built step plans for file creation, code execution, and read/search tasks
- Falls back to LLM planner when no template matches

### 2.3 Local Completion Reports

Generate completion reports locally from task data (no LLM call):

- Used for template-matched tasks
- LLM-planned tasks still use LLM reports (with retry/backoff)

---

## 3. Retry/Backoff

### 3.1 `llm.py` Changes

```python
async def chat(self, messages, tools=None, max_retries=3):
    for attempt in range(max_retries + 1):
        resp = await self._client.post(...)
        if resp.status_code == 429:
            if attempt < max_retries:
                delay = 2 ** (attempt + 1)  # 2, 4, 8
                logger.warning(f"Rate limited (429), retrying in {delay}s...")
                await asyncio.sleep(delay)
                continue
        # handle other errors as before
```

### 3.2 Coverage

| Call site | Where | Retry applies |
|-----------|-------|---------------|
| `planner.generate_plan()` | `planner.py` | ✅ via `llm.chat()` |
| `_execute_step()` tool loop | `task_executor.py` | ✅ via `llm.chat()` |
| `_generate_completion_report()` | `task_executor.py` | ✅ via `llm.chat()` |
| Agent `run_stream()` | `agent.py` | ✅ via `llm.chat()` |

---

## 4. Template Engine

### 4.1 Templates

| Template | Trigger keywords | Plan |
|----------|-----------------|------|
| `file_create` | "create", "write", "make", "build" + file extension | 1. `write_file` the code 2. `run_code` to verify |
| `code_exec` | "run", "execute", "count", "calculate", "print" | 1. `write_file` the script 2. `run_code` to execute |
| `read_search` | "read", "find", "search", "look for" | 1. `search_files` or `read_file` 2. return results |

### 4.2 `TaskTemplateEngine`

```python
class TaskTemplateEngine:
    def match(self, title: str, description: str) -> list[dict] | None:
        text = f"{title} {description}".lower()
        
        if any(kw in text for kw in ["create", "write", "make", "build"]):
            return _file_create_plan(text)
        if any(kw in text for kw in ["run", "execute", "count", "calculate"]):
            return _code_exec_plan(text)
        if any(kw in text for kw in ["read", "find", "search"]):
            return _read_search_plan(text)
        return None
```

### 4.3 Plan Generation

Each template generates steps dynamically based on the task description:

```python
def _code_exec_plan(text: str) -> list[dict]:
    # Extract the task description to generate relevant steps
    return [
        {"step": 1, "title": "Write the script", "description": "...", "status": "pending"},
        {"step": 2, "title": "Execute and verify", "description": "...", "status": "pending"},
    ]
```

### 4.4 Integration

In `task_executor.py`, before calling `self.planner.generate_plan(task)`:

```python
if self.template_engine:
    plan = self.template_engine.match(task["title"], task.get("description", ""))
if plan is None:
    plan = await self.planner.generate_plan(task)
```

---

## 5. Local Completion Report

```python
def _generate_completion_report_local(self, task, plan, artifacts):
    return {
        "title": task["title"][:60],
        "summary": f"Task completed: {task['title']}",
        "key_results": [f"Completed {len(plan)} steps"],
        "files_created": [a["path"] for a in artifacts if a["artifact_type"] == "write_file"],
        "files_modified": [a["path"] for a in artifacts if a["artifact_type"] == "edit_file"],
        "status_emoji": "✓",
        "status_label": "Completed",
        "steps_completed": len(plan),
        "total_steps": len(plan),
    }
```

Used when: template matched. LLM reports still used when: LLM planned.

---

## 6. LLM Call Comparison

| Scenario | Before | After |
|----------|--------|-------|
| Template match (3-step) | 5 calls | **0 calls** |
| No match (3-step) | 5 calls | **3 calls** |
| 429 error | Crash | **Retry 3x**, then fail |

---

## 7. Testing Strategy

| Test | What |
|------|------|
| `test_llm_429_retry` | Verify chat() retries 3 times on 429 then raises |
| `test_llm_other_error_no_retry` | Verify chat() raises immediately on non-429 |
| `test_template_file_create` | Match "create a calculator.py" → file_create plan |
| `test_template_code_exec` | Match "run a script that counts" → code_exec plan |
| `test_template_read_search` | Match "find all .py files" → read_search plan |
| `test_template_no_match` | Novel task returns None → falls back to LLM |
| `test_local_completion_report` | Returns correct structure without LLM |
| `test_executor_uses_template` | TaskExecutor uses template when available |
| `test_executor_falls_back_to_llm` | TaskExecutor uses LLM planner when no template |

---

## 8. Files to Create/Modify

| File | Action | Purpose |
|------|--------|---------|
| `ares/llm.py` | Modify | Add 429 retry/backoff to `chat()` and `chat_stream()` |
| `ares/task_templates.py` | Create | `TaskTemplateEngine` class |
| `ares/task_executor.py` | Modify | Integrate template engine + local reports |
| `tests/test_task_templates.py` | Create | Template matching tests |
| `tests/test_llm_retry.py` | Create | 429 retry tests |

---

## 9. Risks

| Risk | Mitigation |
|------|------------|
| Template too rigid for complex tasks | Falls back to LLM planner — templates are an optimization, not a replacement |
| Retry adds latency on 429 | Only 2-8s delay, better than crashing |
| Local reports lower quality | Only used for template tasks; LLM tasks still get polished reports |
