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

CATEGORY_KEYWORDS: dict[str, list[str]] = {
    "research": [
        "research", "find out", "look up", "search for",
        "what is", "how to", "investigate", "compare",
        "summarize", "difference", "between", "explain",
        "analyze", "review", "survey", "overview",
    ],
    "file": [
        "create file", "write file", "find file", "list files",
        "check file", "read file", "open file", "the file",
        "find the", "list the", "read the", "check the",
    ],
    "memory": [
        "remind me", "what did i say", "recall", "remember",
        "what do i know about", "summarize what",
    ],
}

ALLOWED_TOOLS = {
    "web_search", "fetch_url",
    "read_file", "search_files", "list_directory",
    "glob_pattern", "get_file_info", "head_file", "tail_file",
    "count_lines", "file_tree",
    "search_memory", "store_memory",
    "write_file", "edit_file", "create_directory",
}

MAX_RETRIES = 3

EXECUTOR_STATES = {
    "stopped": "Executor is not running",
    "idle": "Waiting for tasks to execute",
    "scanning": "Scanning for auto-executable tasks",
    "running": "Executing a task",
    "disabled": "Executor is disabled in config",
}


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
            base += f" — currently: \"{self._current_task_title}\""
        if self._last_error:
            base += f" — last error: {self._last_error}"
        return base

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

        prompt = f"""Complete this task: {title}.
If this is a research task, save your findings to a file called 'tasks/{task_id}_{title.lower().replace(' ', '_')[:40]}.md' using write_file.
Write a clear, well-formatted markdown summary of your findings.
If this is a memory task, use store_memory to save the result."""
        if description:
            prompt += f"\nAdditional context: {description}"

        try:
            result = await self.agent_runner(prompt, max_turns)
            return {
                "status": "done",
                "notes": result.get("summary", "Task completed."),
            }
        except Exception as e:
            logger.error("Task execution failed for #%d: %s", task_id, e)
            raise

    async def _process_task(self, task: dict) -> None:
        """Process one task: classify, execute, update, notify."""
        task_id = task["id"]
        title = task["title"]
        self._current_task_id = task_id
        self._current_task_title = title
        self._state = "running"
        self._push_status()
        print(f"[Executor] Processing task #{task_id}: {title}")

        retry_count = task.get("retry_count", 0) or 0
        if retry_count >= MAX_RETRIES:
            logger.warning("Task #%d '%s' exceeded max retries (%d)", task_id, title, MAX_RETRIES)
            self.task_store.update(
                task_id,
                status="partial",
                execution_notes=f"Failed after {MAX_RETRIES} retries. Manual intervention needed.",
                executed_at=now_local_iso(),
            )
            self._tasks_failed += 1
            self._current_task_id = None
            self._current_task_title = None
            return

        self.task_store.update(task_id, status="in_progress")
        logger.info("Executing task #%d: %s", task_id, title)

        category = self._classify_task(title)
        if category is None:
            print(f"[Executor] Task #{task_id} not classifiable, marking partial")
            logger.info("Task #%d '%s' not classifiable, marking partial", task_id, title)
            self.task_store.update(
                task_id,
                status="partial",
                execution_notes="Task type not recognized for auto-execution.",
                executed_at=now_local_iso(),
            )
            self._tasks_failed += 1
            await self._notify(task_id, title, "partial", "Task type not recognized for auto-execution.")
            self._current_task_id = None
            self._current_task_title = None
            return

        print(f"[Executor] Task #{task_id} classified as '{category}', executing...")
        logger.info("Task #%d classified as '%s', executing...", task_id, category)
        try:
            result = await self._execute_task(task)
        except Exception as e:
            new_retry_count = retry_count + 1
            print(f"[Executor] Task #{task_id} failed (attempt {new_retry_count}/{MAX_RETRIES}): {e}")
            logger.error("Task #%d failed (attempt %d/%d): %s", task_id, new_retry_count, MAX_RETRIES, e)
            self._last_error = str(e)
            self.task_store.update(
                task_id,
                status="pending",
                retry_count=new_retry_count,
                execution_notes=f"Execution failed (attempt {new_retry_count}/{MAX_RETRIES}): {e}",
            )
            self._current_task_id = None
            self._current_task_title = None
            return

        now = now_local_iso()
        self.task_store.update(
            task_id,
            status=result["status"],
            execution_notes=result["notes"],
            executed_at=now,
        )

        if result["status"] == "done":
            self._tasks_completed += 1
            print(f"[Executor] Task #{task_id} completed successfully")
            logger.info("Task #%d completed successfully", task_id)
        else:
            self._tasks_failed += 1
            logger.warning("Task #%d completed partially: %s", task_id, result["notes"])

        await self._notify(task_id, title, result["status"], result["notes"])
        self._current_task_id = None
        self._current_task_title = None
        self._push_status()

    def _push_status(self) -> None:
        """Notify the server to push status to all clients."""
        if self.status_callback is not None:
            print(f"[Executor] Pushing status update (state={self._state})")
            result = self.status_callback()
            if inspect.isawaitable(result):
                asyncio.ensure_future(result)

    async def _notify(self, task_id: int, title: str, status: str, notes: str) -> None:
        """Notify the user of a task completion."""
        task_info = {
            "id": task_id,
            "title": title,
            "status": status,
            "notes": notes,
        }
        if self.callback is not None:
            print(f"[Executor] Calling notification callback for task #{task_id}")
            result = self.callback(task_info)
            if inspect.isawaitable(result):
                await result

    async def run_once(self) -> int:
        """Scan and execute all auto-executable tasks once. Returns count processed."""
        if not self.enabled:
            self._state = "disabled"
            print("[Executor] Disabled in config")
            return 0

        self._state = "scanning"
        self._push_status()
        tasks = self.task_store.get_auto_executable()
        print(f"[Executor] Scan found {len(tasks)} auto-executable task(s)")
        processed = 0
        for task in tasks:
            await self._process_task(task)
            processed += 1
            if processed < len(tasks):
                await asyncio.sleep(5)

        self._state = "idle"
        return processed

    async def run(self) -> None:
        """Run the executor loop until cancelled."""
        print(f"[Executor] Loop started (poll every {self.poll_seconds}s)")
        self._started_at = now_local_iso()
        while True:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                print("[Executor] Loop cancelled")
                raise
            except Exception as e:
                self._last_error = str(e)
                self._state = "idle"
                print(f"[Executor] Loop error: {e}")
            await asyncio.sleep(self.poll_seconds)

    def start(self) -> None:
        """Start the background loop as an asyncio task."""
        if self._task is not None:
            print("[Executor] Already running, skipping start")
            logger.warning("Task executor already running")
            return
        if not self.enabled:
            print("[Executor] Disabled in config, not starting")
            logger.info("Task executor is disabled in config")
            self._state = "disabled"
            return
        self._state = "idle"
        self._task = asyncio.create_task(self.run())
        print("[Executor] Started")
        logger.info("Task executor started")

    async def stop(self) -> None:
        """Stop the background loop gracefully."""
        if self._task is not None:
            logger.info("Stopping task executor...")
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
            self._state = "stopped"
            self._current_task_id = None
            self._current_task_title = None
            logger.info("Task executor stopped")
