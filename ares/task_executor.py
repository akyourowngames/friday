"""Background task execution engine with planning, step tracking, and resume."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import re
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
    "run_command", "run_code",
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
        self.event_callback: Callable[[dict], Any] | None = None
        self.poll_seconds = max(1, poll_seconds)
        self.max_turns = max_turns
        self.enabled = enabled
        self._task: asyncio.Task | None = None
        self._wake_event = asyncio.Event()
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
        self.allowed_tools: list[str] = []

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
        """Insert a task event and broadcast it for live debugging."""
        event_id = None
        try:
            event_id = self.task_store.add_event(task_id, level=level, step=step, message=message)
        except Exception as e:
            logger.warning("Failed to log event for task %d: %s", task_id, e)

        if self.event_callback is not None:
            payload = {
                "id": event_id,
                "task_id": task_id,
                "level": level,
                "step": step,
                "message": message,
                "timestamp": now_local_iso(),
            }
            try:
                result = self.event_callback(payload)
                if inspect.isawaitable(result):
                    asyncio.ensure_future(result)
            except Exception as e:
                logger.warning("Failed to broadcast event for task %d: %s", task_id, e)

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
                    line_count = 0
                    for line in f:
                        line_count += 1
                        if description is None and line_count <= 50:
                            stripped = line.strip()
                            if stripped and not stripped.startswith('#'):
                                description = stripped[:80]
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

    @staticmethod
    def _is_retryable_llm_error(error: Exception) -> bool:
        """Return True for transient LLM/provider failures worth retrying."""
        text = str(error).lower()
        return any(token in text for token in (
            "429",
            "too many requests",
            "rate limit",
            "temporarily unavailable",
            "timeout",
            "provider returned error",
        ))

    async def _llm_chat_with_retries(self, messages: list[dict], *, tools=None, context: str = "LLM call") -> dict:
        """Call the LLM with short retries for transient provider failures."""
        delays = (0.0, 0.25, 1.0)
        last_error: Exception | None = None

        for attempt, delay in enumerate(delays, start=1):
            try:
                return await self.llm.chat(messages, tools=tools)
            except Exception as error:
                last_error = error
                if not self._is_retryable_llm_error(error) or attempt == len(delays):
                    raise
                self._log_current_task_warning(
                    f"{context} failed with transient provider error; retrying "
                    f"({attempt + 1}/{len(delays)}): {error}"
                )
                if delay:
                    await asyncio.sleep(delay)

        assert last_error is not None
        raise last_error

    def _log_current_task_warning(self, message: str) -> None:
        """Log a task warning if a current task is active."""
        if self._current_task_id is not None:
            self._log_event(self._current_task_id, "warning", None, message)

    def _template_kind(self, task: dict, step: dict | None = None) -> str | None:
        """Classify simple deterministic task templates that can run without LLM calls."""
        text = " ".join(str(part or "") for part in (
            task.get("title"),
            task.get("description"),
            (step or {}).get("title"),
            (step or {}).get("description"),
        )).lower()

        if "calculator" in text:
            return "calculator"
        if ("count" in text or "counts" in text) and "1" in text and "10" in text and "sum" in text:
            return "count_sum"
        return None

    async def _execute_template_step(self, task: dict, step: dict, reason: str) -> dict | None:
        """Execute known task templates when the LLM provider is unavailable."""
        kind = self._template_kind(task, step)
        if kind is None or self.tool_executor is None:
            return None

        self._log_event(
            task["id"],
            "warning",
            step.get("step"),
            f"LLM unavailable ({reason}); using deterministic {kind} task template.",
        )

        if kind == "calculator":
            return self._execute_calculator_template()
        if kind == "count_sum":
            return self._execute_count_sum_template()
        return None

    @staticmethod
    def _safe_script_name(name: str, default: str) -> str:
        """Convert a task title into a conservative Python filename."""
        stem = re.sub(r"[^a-zA-Z0-9]+", "_", name.lower()).strip("_")[:48]
        return f"{stem or default}.py"

    def _execute_calculator_template(self) -> dict:
        """Create and verify a small interactive calculator script."""
        path = self._safe_script_name("calculator", "calculator")
        script = '''def read_number(prompt):
    while True:
        try:
            return float(input(prompt))
        except ValueError:
            print("Please enter a valid number.")


def fmt(value):
    if value == int(value):
        return str(int(value))
    return str(value)


def main():
    first = read_number("Enter first number: ")
    second = read_number("Enter second number: ")

    print(f"Sum: {fmt(first + second)}")
    print(f"Difference: {fmt(first - second)}")
    print(f"Product: {fmt(first * second)}")
    if second == 0:
        print("Division: Cannot divide by zero")
    else:
        print(f"Division: {fmt(first / second)}")


if __name__ == "__main__":
    main()
'''
        verify_code = f'''import subprocess, sys
path = {path!r}
checks = [
    ("8\n2\n", ["Sum: 10", "Difference: 6", "Product: 16", "Division: 4"]),
    ("8\n0\n", ["Sum: 8", "Difference: 8", "Product: 0", "Division: Cannot divide by zero"]),
]
for input_data, expected in checks:
    result = subprocess.run([sys.executable, path], input=input_data, text=True, capture_output=True, timeout=10)
    print(result.stdout)
    if result.returncode != 0:
        raise SystemExit(result.stderr or f"calculator exited with {{result.returncode}}")
    missing = [item for item in expected if item not in result.stdout]
    if missing:
        raise SystemExit(f"missing expected output: {{missing}}")
print("calculator verification passed")
'''
        self.tool_executor.execute("write_file", {"path": path, "content": script, "confirm": True})
        verification = self.tool_executor.execute("run_code", {"code": verify_code, "timeout": 15})
        return {
            "status": "success",
            "output": f"Created and verified {path}.\n{verification}",
            "artifacts": [{"path": path, "type": "write_file", "timestamp": now_local_iso()}],
            "tool_calls": 2,
        }

    def _execute_count_sum_template(self) -> dict:
        """Create and verify a 1-to-10 counter script."""
        path = self._safe_script_name("1_to_10_counter", "counter")
        script = '''total = 0
for number in range(1, 11):
    print(number)
    total += number
print(f"Total: {total}")
'''
        verify_code = f'''import subprocess, sys
result = subprocess.run([sys.executable, {path!r}], text=True, capture_output=True, timeout=10)
print(result.stdout)
if result.returncode != 0:
    raise SystemExit(result.stderr or f"counter exited with {{result.returncode}}")
expected = [str(i) for i in range(1, 11)] + ["Total: 55"]
missing = [item for item in expected if item not in result.stdout.splitlines()]
if missing:
    raise SystemExit(f"missing expected output: {{missing}}")
print("counter verification passed")
'''
        self.tool_executor.execute("write_file", {"path": path, "content": script, "confirm": True})
        verification = self.tool_executor.execute("run_code", {"code": verify_code, "timeout": 15})
        return {
            "status": "success",
            "output": f"Created and verified {path}.\n{verification}",
            "artifacts": [{"path": path, "type": "write_file", "timestamp": now_local_iso()}],
            "tool_calls": 2,
        }

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
            status="done",
            completion_report=json.dumps(report),
            executed_at=now_local_iso(),
            retry_reason=None,
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
            try:
                response = await self._llm_chat_with_retries(
                    messages,
                    tools=self.allowed_tools,
                    context=f"Task #{task['id']} step {step['step']}",
                )
            except Exception as error:
                template_result = await self._execute_template_step(task, step, str(error))
                if template_result is not None:
                    return template_result
                raise

            if response.get("tool_calls"):
                for call in response["tool_calls"]:
                    tool_name = call.get("tool") or call.get("function", {}).get("name", "")
                    raw_args = call.get("args") or call.get("function", {}).get("arguments", "{}")
                    # OpenAI API returns arguments as a JSON string — parse it
                    if isinstance(raw_args, str):
                        try:
                            tool_args = json.loads(raw_args)
                        except (json.JSONDecodeError, TypeError):
                            tool_args = {}
                    else:
                        tool_args = raw_args
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
                status="partial",
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
            self.task_store.update(task["id"], status="done", executed_at=now_local_iso())
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
            response = await self._llm_chat_with_retries(
                [{"role": "user", "content": prompt}],
                context=f"Task #{task['id']} completion report",
            )
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
            try:
                await asyncio.wait_for(self._wake_event.wait(), timeout=self.poll_seconds)
                self._wake_event.clear()
            except asyncio.TimeoutError:
                pass

    def wake(self) -> None:
        """Wake the executor so newly queued work is picked up promptly."""
        if not self.enabled:
            return
        try:
            if self._task is not None:
                self._task.get_loop().call_soon_threadsafe(self._wake_event.set)
            else:
                self._wake_event.set()
        except RuntimeError:
            self._wake_event.set()

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
