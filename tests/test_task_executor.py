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
    executor.planner = None

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
        store.update(task_id, max_turns=5)
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
        store.update(task_id, max_turns=5)
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


class TestTemplateFallbackExecution:
    def test_execute_step_uses_calculator_template_after_429(self, tmp_path):
        store = _make_store(tmp_path)
        task_id = _make_task(store, title="Build a small Python calculator")
        store.update(task_id, max_turns=5, total_steps=1)
        task = store.get(task_id)

        mock_llm = AsyncMock()
        mock_llm.chat = AsyncMock(side_effect=Exception("LLM API error 429: Too many requests"))
        mock_tool = MagicMock()
        mock_tool.execute = MagicMock(side_effect=[
            "Wrote calculator.py",
            "calculator verification passed",
        ])
        executor = _make_executor(store, mock_llm, mock_tool)
        step = {"step": 1, "title": "Build calculator", "description": "write and verify calculator", "status": "pending"}

        with patch("ares.task_executor.asyncio.sleep", new_callable=AsyncMock) as sleep_mock:
            result = asyncio.run(executor._execute_step(task, step))

        assert result["status"] == "success"
        assert result["artifacts"] == [{"path": "calculator.py", "type": "write_file", "timestamp": result["artifacts"][0]["timestamp"]}]
        assert result["tool_calls"] == 2
        assert mock_tool.execute.call_args_list[0].args[0] == "write_file"
        assert mock_tool.execute.call_args_list[1].args[0] == "run_code"
        assert sleep_mock.await_count == 1

    def test_process_task_completes_calculator_template_when_llm_is_rate_limited(self, tmp_path):
        store = _make_store(tmp_path)
        task_id = _make_task(store, title="Build a small Python calculator")
        store.update(task_id, max_turns=5, max_attempts=3)
        task = store.get(task_id)

        mock_llm = AsyncMock()
        mock_llm.chat = AsyncMock(side_effect=Exception("LLM API error 429: Too many requests"))
        mock_tool = MagicMock()
        mock_tool.execute = MagicMock(side_effect=[
            "Wrote calculator.py",
            "calculator verification passed",
        ])
        executor = _make_executor(store, mock_llm, mock_tool)

        with patch("ares.task_executor.asyncio.sleep", new_callable=AsyncMock):
            asyncio.run(executor._process_task(task))

        updated = store.get(task_id)
        artifacts = store.get_artifacts(task_id)
        assert updated["state"] == "completed"
        assert len(artifacts) == 1
        assert artifacts[0]["path"] == "calculator.py"
        assert executor.callback.call_args.args[0]["status"] == "completed"
        assert "calculator.py" in (updated.get("completion_report") or "")


class TestHandleFailure:
    @pytest.mark.asyncio
    async def test_retry_on_first_failure(self, tmp_path):
        store = _make_store(tmp_path)
        task_id = _make_task(store)
        store.update(task_id, attempt=1, max_attempts=3)
        task = store.get(task_id)

        executor = _make_executor(store, AsyncMock())

        with patch("ares.task_executor.asyncio.sleep", new_callable=AsyncMock):
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


class TestToolExecutorHandlers:
    def _make_executor(self, tmp_path):
        from ares.tools.executor import ToolExecutor
        from ares.memory import MemoryStore
        memory = MemoryStore(db_path=tmp_path / "memory.db")
        store = TaskStore(db_path=tmp_path / "test.db")
        te = ToolExecutor(memory_store=memory, task_store=store)
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
