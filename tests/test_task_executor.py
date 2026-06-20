"""Tests for proactive task execution."""
import asyncio
from unittest.mock import AsyncMock


def _run(coro):
    """Run a coroutine, creating an event loop if needed."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    return loop.run_until_complete(coro)

import pytest

from ares.task_executor import TaskExecutor
from ares.tasks import TaskStore
from ares.memory import MemoryStore
from ares.tools import ToolExecutor


# ── Column existence tests ──────────────────────────────────────

def test_task_store_has_auto_executable_column(tmp_path):
    store = TaskStore(db_path=tmp_path / "test.db")
    task_id = store.create("Test task", auto_executable="yes")
    task = store.get(task_id)
    assert task["auto_executable"] == "yes"
    store.close()


def test_task_store_has_execution_notes_column(tmp_path):
    store = TaskStore(db_path=tmp_path / "test.db")
    task_id = store.create("Test task")
    store.update(task_id, execution_notes="Did research, found 3 articles.")
    task = store.get(task_id)
    assert task["execution_notes"] == "Did research, found 3 articles."
    store.close()


def test_task_store_has_executed_at_column(tmp_path):
    store = TaskStore(db_path=tmp_path / "test.db")
    task_id = store.create("Test task")
    store.update(task_id, executed_at="2026-06-19T12:00:00")
    task = store.get(task_id)
    assert task["executed_at"] == "2026-06-19T12:00:00"
    store.close()


def test_task_store_has_max_turns_column(tmp_path):
    store = TaskStore(db_path=tmp_path / "test.db")
    task_id = store.create("Test task")
    task = store.get(task_id)
    assert task["max_turns"] == 10
    store.close()


def test_task_store_has_retry_count_column(tmp_path):
    store = TaskStore(db_path=tmp_path / "test.db")
    task_id = store.create("Test task")
    task = store.get(task_id)
    assert task["retry_count"] == 0
    store.close()


# ── Classification tests ───────────────────────────────────────

def test_classify_task_research():
    executor = TaskExecutor.__new__(TaskExecutor)
    assert executor._classify_task("Research Python async patterns") == "research"
    assert executor._classify_task("Find out about database migrations") == "research"
    assert executor._classify_task("Look up the best testing frameworks") == "research"
    assert executor._classify_task("What is the capital of France") == "research"
    assert executor._classify_task("Search for articles about AI safety") == "research"


def test_classify_task_file():
    executor = TaskExecutor.__new__(TaskExecutor)
    assert executor._classify_task("Create file called notes.md") == "file"
    assert executor._classify_task("Find the config file") == "file"
    assert executor._classify_task("List files in the project") == "file"


def test_classify_task_memory():
    executor = TaskExecutor.__new__(TaskExecutor)
    assert executor._classify_task("Remind me about the meeting") == "memory"
    assert executor._classify_task("What did I say about the project") == "memory"


def test_classify_task_unknown():
    executor = TaskExecutor.__new__(TaskExecutor)
    assert executor._classify_task("Buy groceries") is None
    assert executor._classify_task("Call the dentist") is None


# ── run_once tests ──────────────────────────────────────────────

def test_run_once_processes_auto_executable_tasks(tmp_path):
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

    count = _run(executor.run_once())
    assert count == 1
    assert len(executed) == 1
    assert "Research AI trends" in executed[0]

    task = store.get(1)
    assert task["status"] == "done"
    assert "Found 3 articles" in task["execution_notes"]
    assert task["executed_at"] is not None
    store.close()


def test_run_once_skips_unknown_tasks(tmp_path):
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

    count = _run(executor.run_once())
    assert count == 1

    task = store.get(1)
    assert task["status"] == "partial"
    assert "not recognized" in task["execution_notes"]
    store.close()


def test_run_once_disabled_does_nothing(tmp_path):
    store = TaskStore(db_path=tmp_path / "test.db")
    store.create("Research something", auto_executable="yes")

    executor = TaskExecutor(
        task_store=store,
        agent_runner=AsyncMock(),
        enabled=False,
    )

    count = _run(executor.run_once())
    assert count == 0
    store.close()


def test_run_once_retries_on_failure(tmp_path):
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

    _run(executor.run_once())
    task = store.get(1)
    assert task["status"] == "pending"
    assert task["retry_count"] == 1
    assert call_count == 1

    _run(executor.run_once())
    _run(executor.run_once())
    assert call_count == 3

    _run(executor.run_once())
    task = store.get(1)
    assert task["execution_notes"] == "Failed after 3 retries. Manual intervention needed."
    store.close()


# ── create_task tool test ───────────────────────────────────────

def test_create_task_with_auto_executable(tmp_path):
    memory = MemoryStore(db_path=tmp_path / "memory.db")
    tasks = TaskStore(db_path=tmp_path / "tasks.db")
    executor = ToolExecutor(memory_store=memory, task_store=tasks)

    result = executor.execute("create_task", {
        "title": "Research Python async patterns",
        "auto_executable": True,
    })
    assert "Created task" in result
    assert "[auto]" in result

    task = tasks.get(1)
    assert task["auto_executable"] == "yes"
    memory.close()
    tasks.close()


# ── End-to-end integration test ─────────────────────────────────

def test_full_workflow_end_to_end(tmp_path):
    from ares.memory import MemoryStore

    memory = MemoryStore(db_path=tmp_path / "memory.db")
    store = TaskStore(db_path=tmp_path / "tasks.db")
    tool_executor = ToolExecutor(memory_store=memory, task_store=store)

    result = tool_executor.execute("create_task", {
        "title": "Research Python async patterns",
        "description": "Find best practices for asyncio",
        "auto_executable": True,
    })
    assert "Created task" in result
    assert "[auto]" in result

    auto_tasks = store.get_auto_executable()
    assert len(auto_tasks) == 1
    assert auto_tasks[0]["title"] == "Research Python async patterns"

    executed = []

    async def fake_runner(prompt, max_turns):
        executed.append(prompt)
        return {"summary": "Found 3 articles about asyncio patterns."}

    executor = TaskExecutor(
        task_store=store,
        agent_runner=fake_runner,
        enabled=True,
    )
    _run(executor.run_once())

    task = store.get(1)
    assert task["status"] == "done"
    assert "Found 3 articles" in task["execution_notes"]
    assert task["executed_at"] is not None

    history = store.get_recently_executed()
    assert len(history) == 1
    assert history[0]["title"] == "Research Python async patterns"

    auto_tasks = store.get_auto_executable()
    assert len(auto_tasks) == 0

    status_result = tool_executor.execute("get_execution_status", {"limit": 10})
    assert "Research Python async patterns" in status_result
    assert "\u2705" in status_result

    memory.close()
    store.close()
