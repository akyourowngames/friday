# Task Executor LLM Optimization — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Eliminate 429 rate-limit crashes and reduce LLM calls for auto-executed tasks from 5-15 down to 0-3.

**Architecture:** Three-layer optimization: (1) exponential backoff on 429 errors in `llm.py`, (2) `TaskTemplateEngine` for keyword-based plan generation without LLM, (3) local completion reports for template-matched tasks.

**Tech Stack:** Python stdlib only.

---

## File Map

| File | Action | Purpose |
|------|--------|---------|
| `ares/llm.py:167-193,195-224` | **Modify** | Add 429 retry/backoff to `chat()` and `chat_stream()` |
| `ares/task_templates.py` | **Create** | `TaskTemplateEngine` class with keyword matching |
| `ares/task_executor.py:194-212,407-449` | **Modify** | Integrate template engine + local completion reports |
| `tests/test_llm_retry.py` | **Create** | 429 retry tests |
| `tests/test_task_templates.py` | **Create** | Template matching tests |

---

### Task 1: Write failing tests for 429 retry

**Files:**
- Create: `tests/test_llm_retry.py`

- [ ] **Step 1: Create test file**

```python
"""Tests for LLM 429 retry/backoff."""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


@pytest.fixture
def mock_client():
    """Create an LLMClient with a mocked httpx client."""
    from ares.llm import LLMClient
    client = LLMClient(api_key="test-key", base_url="https://test.api.com", model="test-model")
    return client


def _make_response(status_code, text="error"):
    """Create a mock httpx response."""
    resp = AsyncMock()
    resp.status_code = status_code
    resp.text = text

    async def aiter_text():
        yield text

    resp.aiter_text = aiter_text
    return resp


class TestLLM429Retry:

    @pytest.mark.asyncio
    async def test_429_retries_three_times(self, mock_client):
        """On 429, should retry 3 times before raising."""
        mock_client._client.post = AsyncMock(
            return_value=_make_response(429, '{"error":{"message":"rate limited"}}')
        )

        with pytest.raises(Exception, match="429"):
            await mock_client.chat([{"role": "user", "content": "test"}])

        # Initial request + 3 retries = 4 total calls
        assert mock_client._client.post.call_count == 4

    @pytest.mark.asyncio
    async def test_non_429_raises_immediately(self, mock_client):
        """On non-429 error, should raise immediately (no retry)."""
        mock_client._client.post = AsyncMock(
            return_value=_make_response(500, "server error")
        )

        with pytest.raises(Exception, match="500"):
            await mock_client.chat([{"role": "user", "content": "test"}])

        assert mock_client._client.post.call_count == 1

    @pytest.mark.asyncio
    async def test_retry_succeeds_on_second_attempt(self, mock_client):
        """Should succeed if second attempt returns 200."""
        resp_429 = _make_response(429, "rate limited")
        resp_ok = MagicMock()
        resp_ok.status_code = 200
        resp_ok.json.return_value = {"choices": [{"message": {"content": "ok"}}]}

        mock_client._client.post = AsyncMock(side_effect=[resp_429, resp_ok])

        result = await mock_client.chat([{"role": "user", "content": "test"}])
        assert result["content"] == "ok"
        assert mock_client._client.post.call_count == 2

    @pytest.mark.asyncio
    async def test_success_no_retry(self, mock_client):
        """On 200, should not retry at all."""
        resp_ok = MagicMock()
        resp_ok.status_code = 200
        resp_ok.json.return_value = {"choices": [{"message": {"content": "hello"}}]}

        mock_client._client.post = AsyncMock(return_value=resp_ok)

        result = await mock_client.chat([{"role": "user", "content": "test"}])
        assert result["content"] == "hello"
        assert mock_client._client.post.call_count == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_llm_retry.py -v --tb=short 2>&1 | head -30`
Expected: Tests fail (no retry logic exists yet — all should pass on first attempt or fail differently)

- [ ] **Step 3: Commit**

```bash
git add tests/test_llm_retry.py
git commit -m "test: add failing tests for LLM 429 retry/backoff"
```

---

### Task 2: Implement 429 retry in `llm.py`

**Files:**
- Modify: `ares/llm.py:167-193`

- [ ] **Step 1: Add `import asyncio` at top of file**

At line 4 of `ares/llm.py`, add after `import json`:

```python
import asyncio
import logging

logger = logging.getLogger(__name__)
```

- [ ] **Step 2: Rewrite `chat()` with retry loop**

Replace the `chat()` method (lines 167-193) with:

```python
    async def chat(self, messages: list[dict], tools: list[dict] | None = None,
                   tool_choice: str = "auto", max_retries: int = 3) -> dict:
        """Send a chat completion request. Retries on 429 with exponential backoff."""
        messages = self._sanitize_tool_call_ids(messages)
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": 0.3,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = tool_choice

        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        last_error = None
        for attempt in range(max_retries + 1):
            resp = await self._client.post(
                f"{self.base_url}/chat/completions",
                json=payload,
                headers=headers,
            )
            if resp.status_code == 429:
                last_error = Exception(f"LLM API error 429: {resp.text[:1000]}")
                if attempt < max_retries:
                    delay = 2 ** (attempt + 1)  # 2, 4, 8 seconds
                    logger.warning("Rate limited (429), retrying in %ds (attempt %d/%d)...",
                                   delay, attempt + 1, max_retries)
                    await asyncio.sleep(delay)
                    continue
            if resp.status_code != 200:
                raise Exception(f"LLM API error {resp.status_code}: {resp.text[:1000]}")
            data = resp.json()
            return data["choices"][0]["message"]

        raise last_error  # Should not reach here, but safety net
```

- [ ] **Step 3: Run retry tests**

Run: `python -m pytest tests/test_llm_retry.py -v --tb=short 2>&1 | head -30`
Expected: All 4 tests PASS

- [ ] **Step 4: Commit**

```bash
git add ares/llm.py
git commit -m "feat: add 429 retry/backoff to LLMClient.chat()"
```

---

### Task 3: Write failing tests for TaskTemplateEngine

**Files:**
- Create: `tests/test_task_templates.py`

- [ ] **Step 1: Create test file**

```python
"""Tests for TaskTemplateEngine."""

import pytest
from ares.task_templates import TaskTemplateEngine


@pytest.fixture
def engine():
    return TaskTemplateEngine()


class TestTaskTemplateEngine:

    def test_file_create_match(self, engine):
        result = engine.match("Create a calculator.py file", "Write a Python calculator")
        assert result is not None
        assert len(result) >= 1
        # Should contain write_file step
        titles = [s["title"].lower() for s in result]
        assert any("write" in t or "create" in t or "file" in t for t in titles)

    def test_code_exec_match(self, engine):
        result = engine.match("Run a script that counts to 10", "Execute Python code")
        assert result is not None
        assert len(result) >= 1
        # Should contain run_code or execute step
        titles = [s["title"].lower() for s in result]
        assert any("run" in t or "execut" in t for t in titles)

    def test_read_search_match(self, engine):
        result = engine.match("Find all Python files in the project", "Search for .py files")
        assert result is not None
        assert len(result) >= 1

    def test_no_match_returns_none(self, engine):
        result = engine.match("Do something completely novel and unique", "No keywords match here xyz")
        assert result is None

    def test_plan_has_required_fields(self, engine):
        result = engine.match("Create a test.py file", "Write a test script")
        assert result is not None
        for step in result:
            assert "step" in step
            assert "title" in step
            assert "description" in step
            assert "status" in step
            assert step["status"] == "pending"

    def test_plan_steps_sequential(self, engine):
        result = engine.match("Create hello.py", "Write a hello world script")
        assert result is not None
        for i, step in enumerate(result):
            assert step["step"] == i + 1

    def test_case_insensitive(self, engine):
        result = engine.match("CREATE A FILE", "WRITE SOMETHING")
        assert result is not None

    def test_file_create_with_description(self, engine):
        result = engine.match(
            "Build a todo list app",
            "Create a Python script with add, remove, and list functions"
        )
        assert result is not None
        assert len(result) >= 2
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_task_templates.py -v --tb=short 2>&1 | head -20`
Expected: FAIL with `ModuleNotFoundError: No module named 'ares.task_templates'`

- [ ] **Step 3: Commit**

```bash
git add tests/test_task_templates.py
git commit -m "test: add failing tests for TaskTemplateEngine"
```

---

### Task 4: Implement TaskTemplateEngine

**Files:**
- Create: `ares/task_templates.py`

- [ ] **Step 1: Create the template engine**

```python
"""Template-based task planner — generates plans without LLM calls."""

from __future__ import annotations

import re


class TaskTemplateEngine:
    """Matches task titles/descriptions to pre-built plan templates."""

    def match(self, title: str, description: str = "") -> list[dict] | None:
        """Try to match a task to a template. Returns plan or None."""
        text = f"{title} {description}".lower()

        if _has_any(text, ["create file", "write file", "create a", "write a",
                           "make a", "build a", "build file"]):
            return _file_create_plan(text)

        if _has_any(text, ["run script", "run code", "execute", "run a",
                           "count", "calculate", "print", "run it"]):
            return _code_exec_plan(text)

        if _has_any(text, ["read file", "find file", "search", "find all",
                           "look for", "read all"]):
            return _read_search_plan(text)

        return None


def _has_any(text: str, keywords: list[str]) -> bool:
    """Check if any keyword is in text."""
    return any(kw in text for kw in keywords)


def _file_create_plan(text: str) -> list[dict]:
    """Plan for file creation tasks."""
    # Extract filename if mentioned
    file_match = re.search(r"[\w/\\.-]+\.\w{1,5}", text)
    filename = file_match.group(0) if file_match else "the file"

    return [
        {
            "step": 1,
            "title": f"Write {filename}",
            "description": f"Create the code for {filename} as described in the task.",
            "status": "pending",
        },
        {
            "step": 2,
            "title": "Verify the file works",
            "description": f"Run or test {filename} to confirm it works correctly.",
            "status": "pending",
        },
    ]


def _code_exec_plan(text: str) -> list[dict]:
    """Plan for code execution tasks."""
    # Extract filename if mentioned
    file_match = re.search(r"[\w/\\.-]+\.\w{1,5}", text)
    filename = file_match.group(0) if file_match else "script.py"

    return [
        {
            "step": 1,
            "title": f"Write {filename}",
            "description": f"Write the code for {filename} as described in the task.",
            "status": "pending",
        },
        {
            "step": 2,
            "title": "Execute and verify output",
            "description": f"Run {filename} and verify the output matches expectations.",
            "status": "pending",
        },
    ]


def _read_search_plan(text: str) -> list[dict]:
    """Plan for read/search tasks."""
    return [
        {
            "step": 1,
            "title": "Search and read files",
            "description": "Search for the relevant files and read their contents.",
            "status": "pending",
        },
        {
            "step": 2,
            "title": "Summarize findings",
            "description": "Summarize the relevant information found.",
            "status": "pending",
        },
    ]
```

- [ ] **Step 2: Run template tests**

Run: `python -m pytest tests/test_task_templates.py -v --tb=short 2>&1 | head -30`
Expected: All 8 tests PASS

- [ ] **Step 3: Commit**

```bash
git add ares/task_templates.py
git commit -m "feat: add TaskTemplateEngine for LLM-free plan generation"
```

---

### Task 5: Integrate templates and local reports into TaskExecutor

**Files:**
- Modify: `ares/task_executor.py:38-76` (init + attributes)
- Modify: `ares/task_executor.py:194-221` (planning phase)
- Modify: `ares/task_executor.py:267-275` (completion phase)
- Modify: `ares/task_executor.py:407-449` (report generation)

- [ ] **Step 1: Add template engine import and attribute**

In `ares/task_executor.py`, add at the top after existing imports (around line 12):

```python
from ares.task_templates import TaskTemplateEngine
```

In `__init__` (around line 72), add after `self.allowed_tools`:

```python
        self.template_engine = TaskTemplateEngine()
        self._use_llm_plan: set[int] = set()  # tasks that fell back to LLM planning
```

- [ ] **Step 2: Modify planning phase in `_process_task()`**

Replace lines 204-221 (the planning section) with:

```python
            # Phase 1: Planning — try template first, fall back to LLM
            self._set_state(task_id, "planning")
            self._log_event(task_id, "info", None, "Generating execution plan...")

            plan = self.template_engine.match(task.get("title", ""), task.get("description", "") or "")
            if plan is not None:
                self._log_event(task_id, "info", None, f"Matched template: {len(plan)} steps (0 LLM calls)")
            else:
                # No template match — fall back to LLM planner
                self._use_llm_plan.add(task_id)
                if self.planner:
                    plan = await self.planner.generate_plan(task)
                else:
                    plan = [{"step": 1, "title": title, "description": task.get("description", ""), "status": "pending"}]

            self.task_store.update(task_id,
                state="planning",
                plan=json.dumps(plan),
                total_steps=len(plan),
                current_step=0,
                completed_steps=json.dumps([]),
            )
            self._log_event(task_id, "success", None, f"Plan ready: {len(plan)} steps")
```

- [ ] **Step 3: Modify completion phase in `_execute_steps_from()`**

Replace lines 267-275 (the completion section after all steps) with:

```python
        # All steps done — generate completion report
        self._log_event(task_id, "info", None, "Generating completion report...")
        if task_id in self._use_llm_plan:
            report = await self._generate_completion_report(task, plan, tool_call_count)
            self._use_llm_plan.discard(task_id)
        else:
            report = self._generate_completion_report_local(task, plan, tool_call_count)
        self.task_store.update(task_id,
            state="completed",
            completion_report=json.dumps(report),
            executed_at=now_local_iso(),
        )
        self._log_event(task_id, "success", None, "Task completed successfully")
```

- [ ] **Step 4: Add local completion report method**

Add this new method after `_generate_completion_report()` (after line 449):

```python
    def _generate_completion_report_local(self, task: dict, plan: list[dict], tool_call_count: int) -> dict:
        """Generate a completion report without LLM calls."""
        artifacts = self.task_store.get_artifacts(task["id"])
        report = {
            "title": task["title"][:60],
            "summary": f"Task completed: {task['title']}",
            "key_results": [f"Completed {len(plan)} steps"],
            "file_descriptions": {},
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
```

- [ ] **Step 5: Run all tests**

Run: `python -m pytest tests/test_task_templates.py tests/test_llm_retry.py tests/test_task_executor.py -v --tb=short 2>&1 | head -40`
Expected: All PASS

- [ ] **Step 6: Commit**

```bash
git add ares/task_executor.py
git commit -m "feat: integrate template engine + local reports into TaskExecutor"
```

---

### Task 6: Full test suite verification

- [ ] **Step 1: Run full test suite**

Run: `python -m pytest tests/ -x -q --tb=short 2>&1 | tail -10`
Expected: All tests pass (or only pre-existing unrelated failures)

- [ ] **Step 2: Commit if any cleanup needed**

```bash
git add -A
git commit -m "fix: test cleanup after LLM optimization integration"
```

---

## Success Criteria Checklist

1. ✅ 429 error retries 3 times with exponential backoff, then raises
2. ✅ Non-429 errors raise immediately (no retry)
3. ✅ Template engine matches "create/write/build" → file_create plan (0 LLM calls)
4. ✅ Template engine matches "run/execute/count" → code_exec plan (0 LLM calls)
5. ✅ Template engine matches "find/search/read" → read_search plan (0 LLM calls)
6. ✅ Novel tasks fall back to LLM planner
7. ✅ Template-matched tasks use local completion report (0 LLM calls total)
8. ✅ LLM-planned tasks use LLM completion report with retry
9. ✅ All existing tests pass
10. ✅ No new external dependencies
