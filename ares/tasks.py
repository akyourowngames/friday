"""Durable, revisioned task storage for resumable guarded workflows.

The implementation intentionally follows the cron store's file-lock + atomic
replace design.  A task may be touched by the CLI, desktop app, and a runner in
separate processes, so every mutation is revisioned and active runs carry a
recoverable lease.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from contextlib import suppress
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from ares.cron.store import _exclusive_file_lock
from ares.models import TASK_TRANSITIONS, TaskState


class TaskConflictError(RuntimeError):
    """A mutation is based on stale task state."""


class TaskAlreadyRunningError(TaskConflictError):
    """A second runner tried to take a live task lease."""


class TaskLeaseLostError(TaskConflictError):
    """A runner attempted to update a task after losing its lease."""


_STEP_ID = re.compile(r"[a-z][a-z0-9_-]{0,79}$")
_TASK_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,99}$")
_WHITESPACE = re.compile(r"\s+")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_iso(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _copy(value: Any) -> Any:
    return deepcopy(value)


def _clean(value: Any, *, field: str, limit: int, required: bool = False) -> str:
    text = _WHITESPACE.sub(" ", str(value or "")).strip()
    if required and not text:
        raise ValueError(f"{field} is required")
    if len(text) > limit:
        raise ValueError(f"{field} must be at most {limit} characters")
    return text


def _safe_step_view(step: dict[str, Any], index: int) -> dict[str, Any]:
    """Expose task status without returning stored message bodies/contacts."""
    verify = step.get("verify") if isinstance(step.get("verify"), dict) else None
    return {
        "index": index,
        "step_id": step.get("step_id", f"step-{index + 1}"),
        "tool_name": step.get("tool_name", ""),
        "description": step.get("description", ""),
        "verification_tool": verify.get("tool_name", "") if verify else "",
    }


def task_public_view(task: dict[str, Any], *, include_steps: bool = True) -> dict[str, Any]:
    """Return a model-safe task view that deliberately omits raw arguments."""
    public = {
        "task_id": task["task_id"],
        "goal": task["goal"],
        "status": task["status"],
        "created_at": task["created_at"],
        "updated_at": task["updated_at"],
        "result_summary": task.get("result_summary") or "",
        "current_step": int(task.get("current_step", 0)),
        "total_steps": len(task.get("plan", [])),
        "related_person_ids": list(task.get("related_person_ids", [])),
        "related_action_ids": list(task.get("related_action_ids", [])),
        "session_id": task.get("session_id"),
        "revision": int(task.get("revision", 1)),
        "run_count": int(task.get("run_count", 0)),
        "approval_request": _copy(task.get("approval_request")) if task.get("approval_request") else None,
    }
    if include_steps:
        public["plan"] = [_safe_step_view(step, index) for index, step in enumerate(task.get("plan", []))]
    return public


class TaskStore:
    """Transactional task document with optimistic revisions and leases."""

    _USER_FIELDS = {"goal", "plan", "related_person_ids", "session_id"}

    def __init__(self, data_dir: str | Path | None = None):
        root = Path(data_dir or "~/.ares").expanduser()
        if root.name == "data":
            root = root.parent
        self.root = root
        self.tasks_dir = root / "tasks"
        self.tasks_dir.mkdir(parents=True, exist_ok=True)
        with suppress(Exception):
            self.recover_expired_leases()

    def _tasks_path(self) -> Path:
        return self.tasks_dir / "tasks.json"

    def _lock_path(self) -> Path:
        return self.tasks_dir / "tasks.lock"

    def _read_unlocked(self) -> dict[str, Any]:
        path = self._tasks_path()
        if not path.exists():
            return {"revision": 0, "tasks": {}}
        try:
            data = json.loads(path.read_text(encoding="utf-8") or '{"tasks": {}}')
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Task store is not valid JSON: {path}") from exc
        if not isinstance(data, dict) or not isinstance(data.get("tasks"), dict):
            raise RuntimeError("Task store has an invalid document shape")
        data.setdefault("revision", 0)
        if not isinstance(data["revision"], int) or data["revision"] < 0:
            raise RuntimeError("Task store has an invalid revision")
        return data

    def _write_unlocked(self, data: dict[str, Any]) -> None:
        path = self._tasks_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
        try:
            with temporary.open("w", encoding="utf-8", newline="\n") as handle:
                json.dump(data, handle, indent=2, ensure_ascii=False, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            with suppress(OSError):
                directory_fd = os.open(str(path.parent), os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
        finally:
            with suppress(FileNotFoundError):
                temporary.unlink()

    @staticmethod
    def _plan_fingerprint(plan: list[dict[str, Any]]) -> str:
        """Bind an approval to the exact sensitive plan, including arguments."""
        payload = json.dumps(plan, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @staticmethod
    def _normalize_id(value: str) -> str:
        slug = re.sub(r"[^a-z0-9]+", "-", str(value or "").casefold()).strip("-")
        return slug[:60] or "task"

    def _validate_step(self, value: Any, index: int, known_ids: set[str]) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise ValueError(f"Task step {index + 1} must be an object")
        tool_name = _clean(value.get("tool_name"), field=f"task step {index + 1} tool_name", limit=180, required=True)
        if tool_name == "run_task":
            raise ValueError("A workflow step cannot invoke run_task recursively")
        arguments = value.get("arguments", {})
        if not isinstance(arguments, dict):
            raise ValueError(f"Task step {index + 1} arguments must be an object")
        try:
            json.dumps(arguments, ensure_ascii=False)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Task step {index + 1} arguments must be JSON serializable") from exc
        step_id = str(value.get("step_id") or f"step-{index + 1}").casefold()
        if not _STEP_ID.fullmatch(step_id) or step_id in known_ids:
            raise ValueError(f"Task step {index + 1} has an invalid or duplicate step_id")
        known_ids.add(step_id)
        description = _clean(value.get("description"), field=f"task step {index + 1} description", limit=500)
        verify = value.get("verify")
        if verify is not None:
            if not isinstance(verify, dict):
                raise ValueError(f"Task step {index + 1} verify must be an object")
            verify_tool = _clean(verify.get("tool_name"), field=f"task step {index + 1} verify.tool_name", limit=180, required=True)
            verify_args = verify.get("arguments", {})
            if not isinstance(verify_args, dict):
                raise ValueError(f"Task step {index + 1} verify.arguments must be an object")
            contains = verify.get("contains")
            if contains is not None and not isinstance(contains, (str, list, tuple)):
                raise ValueError(f"Task step {index + 1} verify.contains must be text or a list of text")
            not_contains = verify.get("not_contains")
            if not_contains is not None and not isinstance(not_contains, (str, list, tuple)):
                raise ValueError(f"Task step {index + 1} verify.not_contains must be text or a list of text")
            verify = {
                "tool_name": verify_tool,
                "arguments": _copy(verify_args),
                **({"contains": _copy(contains)} if contains is not None else {}),
                **({"not_contains": _copy(not_contains)} if not_contains is not None else {}),
            }
        return {
            "step_id": step_id,
            "tool_name": tool_name,
            "arguments": _copy(arguments),
            "description": description,
            "verify": verify,
        }

    def _validate_plan(self, plan: Any) -> list[dict[str, Any]]:
        if not isinstance(plan, list) or not plan:
            raise ValueError("plan must be a non-empty ordered list of task steps")
        if len(plan) > 100:
            raise ValueError("plan may contain at most 100 steps")
        known_ids: set[str] = set()
        return [self._validate_step(step, index, known_ids) for index, step in enumerate(plan)]

    @staticmethod
    def _validate_ids(values: Any, *, field: str) -> list[int]:
        if values is None:
            return []
        if not isinstance(values, list):
            raise ValueError(f"{field} must be a list of ids")
        clean: list[int] = []
        for value in values:
            try:
                numeric = int(value)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{field} must contain integer ids") from exc
            if numeric < 1:
                raise ValueError(f"{field} must contain positive ids")
            if numeric not in clean:
                clean.append(numeric)
        return clean

    @staticmethod
    def _lease_expired(task: dict[str, Any], now: datetime | None = None) -> bool:
        expiry = task.get("lease_expires_at")
        if not expiry:
            return True
        try:
            return _parse_iso(str(expiry)) <= (now or datetime.now(timezone.utc))
        except (TypeError, ValueError):
            return True

    def _validate_task(self, task: dict[str, Any]) -> None:
        if not isinstance(task, dict):
            raise ValueError("Task must be an object")
        task_id = str(task.get("task_id") or "")
        if not _TASK_ID.fullmatch(task_id):
            raise ValueError("Task id contains invalid characters")
        _clean(task.get("goal"), field="goal", limit=2_000, required=True)
        task["plan"] = self._validate_plan(task.get("plan"))
        try:
            state = TaskState(str(task.get("status")))
        except ValueError as exc:
            raise ValueError("Task status is invalid") from exc
        if not isinstance(task.get("revision"), int) or int(task["revision"]) < 1:
            raise ValueError("Task revision is invalid")
        for field in ("created_at", "updated_at"):
            _parse_iso(str(task.get(field) or ""))
        current_step = int(task.get("current_step", 0))
        if not 0 <= current_step <= len(task["plan"]):
            raise ValueError("Task current_step is invalid")
        task["related_person_ids"] = self._validate_ids(task.get("related_person_ids"), field="related_person_ids")
        task["related_action_ids"] = self._validate_ids(task.get("related_action_ids"), field="related_action_ids")
        if not isinstance(task.get("run_count"), int) or int(task["run_count"]) < 0:
            raise ValueError("Task run_count is invalid")
        for field in ("lease_expires_at", "last_heartbeat_at", "run_started_at"):
            if task.get(field):
                _parse_iso(str(task[field]))
        if state == TaskState.RUNNING:
            if not task.get("lease_id") or not task.get("lease_expires_at"):
                raise ValueError("Running task is missing a lease")
        if state == TaskState.AWAITING_CONFIRMATION:
            request = task.get("approval_request")
            if not isinstance(request, dict) or not request.get("step_indexes"):
                raise ValueError("Awaiting-confirmation task is missing an approval request")
            if task.get("lease_id") or task.get("lease_expires_at"):
                raise ValueError("Awaiting-confirmation task must not retain a lease")
        if not isinstance(task.get("approved_step_indexes", []), list):
            raise ValueError("Task approved_step_indexes must be a list")
        for index in task.get("approved_step_indexes", []):
            if not isinstance(index, int) or not 0 <= index < len(task["plan"]):
                raise ValueError("Task approved_step_indexes contains an invalid index")

    def _validate_document(self, data: dict[str, Any]) -> None:
        tasks = data.get("tasks")
        if not isinstance(tasks, dict):
            raise ValueError("Task document tasks must be an object")
        for task_id, task in tasks.items():
            if not isinstance(task, dict) or task.get("task_id") != task_id:
                raise ValueError("Task document contains a mismatched task id")
            self._validate_task(task)

    def _transaction(
        self,
        mutation: Callable[[dict[str, Any]], Any],
        *,
        expected_store_revision: int | None = None,
    ) -> Any:
        with _exclusive_file_lock(self._lock_path()):
            data = self._read_unlocked()
            if expected_store_revision is not None and data["revision"] != int(expected_store_revision):
                raise TaskConflictError(
                    f"Stale task store revision {expected_store_revision}; current revision is {data['revision']}."
                )
            result = mutation(data)
            self._validate_document(data)
            data["revision"] += 1
            self._write_unlocked(data)
            return _copy(result)

    def _read(self) -> dict[str, Any]:
        with _exclusive_file_lock(self._lock_path()):
            return _copy(self._read_unlocked())

    @staticmethod
    def _transition(task: dict[str, Any], target: TaskState) -> None:
        current = TaskState(str(task["status"]))
        if target != current and target not in TASK_TRANSITIONS[current]:
            raise ValueError(f"Invalid task transition: {current.value} -> {target.value}")
        task["status"] = target.value

    def create_task(
        self,
        goal: str,
        plan: list[dict[str, Any]],
        *,
        related_person_ids: list[int] | None = None,
        session_id: str | None = None,
        expected_store_revision: int | None = None,
    ) -> dict[str, Any]:
        clean_goal = _clean(goal, field="goal", limit=2_000, required=True)
        clean_plan = self._validate_plan(plan)
        person_ids = self._validate_ids(related_person_ids, field="related_person_ids")

        def mutate(data: dict[str, Any]) -> dict[str, Any]:
            tasks = data.setdefault("tasks", {})
            base = self._normalize_id(clean_goal)
            task_id = f"{base}-{uuid.uuid4().hex[:8]}"
            while task_id in tasks:
                task_id = f"{base}-{uuid.uuid4().hex[:8]}"
            now = utc_now()
            task = {
                "task_id": task_id,
                "goal": clean_goal,
                "plan": _copy(clean_plan),
                "status": TaskState.PENDING.value,
                "created_at": now,
                "updated_at": now,
                "result_summary": "",
                "related_person_ids": person_ids,
                "related_action_ids": [],
                "session_id": _clean(session_id, field="session_id", limit=160) or None,
                "revision": 1,
                "current_step": 0,
                "step_results": [],
                "lease_id": None,
                "lease_expires_at": None,
                "last_heartbeat_at": None,
                "run_started_at": None,
                "run_count": 0,
                "approval_request": None,
                "approved_step_indexes": [],
                "last_error": "",
            }
            tasks[task_id] = task
            return task

        return self._transaction(mutate, expected_store_revision=expected_store_revision)

    def list_tasks(self, *, statuses: list[str] | None = None, limit: int = 50) -> list[dict[str, Any]]:
        data = self._read()
        wanted = {str(status) for status in statuses or []}
        bounded = max(1, min(int(limit), 100))
        tasks = [task for task in data["tasks"].values() if not wanted or task.get("status") in wanted]
        tasks.sort(key=lambda task: (task.get("updated_at", ""), task.get("task_id", "")), reverse=True)
        return [_copy(task) for task in tasks[:bounded]]

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        self.recover_expired_leases()
        return _copy(self._read().get("tasks", {}).get(str(task_id)))

    def update_task(
        self,
        task_id: str,
        *,
        expected_revision: int | None = None,
        **updates: Any,
    ) -> dict[str, Any]:
        unknown = set(updates).difference(self._USER_FIELDS)
        if unknown:
            raise ValueError(f"Unknown or immutable task field(s): {', '.join(sorted(unknown))}")

        def mutate(data: dict[str, Any]) -> dict[str, Any]:
            task = data.setdefault("tasks", {}).get(str(task_id))
            if task is None:
                raise ValueError(f"Task '{task_id}' was not found")
            if expected_revision is not None and int(task["revision"]) != int(expected_revision):
                raise TaskConflictError(f"Task '{task_id}' changed since revision {expected_revision}.")
            if task["status"] in {TaskState.RUNNING.value, TaskState.AWAITING_CONFIRMATION.value}:
                raise TaskConflictError("A running or awaiting-confirmation task cannot be edited; cancel it first.")
            if "goal" in updates and updates["goal"] is not None:
                task["goal"] = _clean(updates["goal"], field="goal", limit=2_000, required=True)
            if "plan" in updates and updates["plan"] is not None:
                task["plan"] = self._validate_plan(updates["plan"])
                task["current_step"] = 0
                task["step_results"] = []
                task["approved_step_indexes"] = []
                task["approval_request"] = None
                if task["status"] == TaskState.FAILED.value:
                    self._transition(task, TaskState.PENDING)
            if "related_person_ids" in updates and updates["related_person_ids"] is not None:
                task["related_person_ids"] = self._validate_ids(updates["related_person_ids"], field="related_person_ids")
            if "session_id" in updates and updates["session_id"] is not None:
                task["session_id"] = _clean(updates["session_id"], field="session_id", limit=160) or None
            task["updated_at"] = utc_now()
            task["revision"] = int(task["revision"]) + 1
            return task

        return self._transaction(mutate)

    def retry_task(self, task_id: str, *, expected_revision: int | None = None) -> dict[str, Any]:
        def mutate(data: dict[str, Any]) -> dict[str, Any]:
            task = data.setdefault("tasks", {}).get(str(task_id))
            if task is None:
                raise ValueError(f"Task '{task_id}' was not found")
            if expected_revision is not None and int(task["revision"]) != int(expected_revision):
                raise TaskConflictError(f"Task '{task_id}' changed since revision {expected_revision}.")
            if task["status"] != TaskState.FAILED.value:
                raise TaskConflictError("Only failed tasks can be retried.")
            self._transition(task, TaskState.PENDING)
            task.update(updated_at=utc_now(), result_summary="", last_error="", revision=int(task["revision"]) + 1)
            return task

        return self._transaction(mutate)

    def cancel_task(self, task_id: str, *, expected_revision: int | None = None) -> dict[str, Any]:
        def mutate(data: dict[str, Any]) -> dict[str, Any]:
            task = data.setdefault("tasks", {}).get(str(task_id))
            if task is None:
                raise ValueError(f"Task '{task_id}' was not found")
            if expected_revision is not None and int(task["revision"]) != int(expected_revision):
                raise TaskConflictError(f"Task '{task_id}' changed since revision {expected_revision}.")
            if task["status"] in {TaskState.COMPLETED.value, TaskState.CANCELLED.value}:
                raise TaskConflictError(f"Task '{task_id}' is already {task['status']}.")
            self._transition(task, TaskState.CANCELLED)
            task.update(
                updated_at=utc_now(),
                result_summary="Cancelled by user.",
                lease_id=None,
                lease_expires_at=None,
                approval_request=None,
                approved_step_indexes=[],
                revision=int(task["revision"]) + 1,
            )
            return task

        return self._transaction(mutate)

    def claim_task(self, task_id: str, *, lease_seconds: int = 900) -> dict[str, Any]:
        duration = max(5, min(int(lease_seconds), 3_600))

        def mutate(data: dict[str, Any]) -> dict[str, Any]:
            task = data.setdefault("tasks", {}).get(str(task_id))
            if task is None:
                raise ValueError(f"Task '{task_id}' was not found")
            now = datetime.now(timezone.utc)
            if task["status"] == TaskState.RUNNING.value and not self._lease_expired(task, now):
                raise TaskAlreadyRunningError(f"Task '{task_id}' is already running")
            if task["status"] == TaskState.AWAITING_CONFIRMATION.value:
                raise TaskConflictError("Task is awaiting explicit confirmation.")
            if task["status"] != TaskState.PENDING.value:
                raise TaskConflictError(f"Task '{task_id}' cannot run while {task['status']}.")
            self._transition(task, TaskState.RUNNING)
            now_text = now.isoformat(timespec="seconds").replace("+00:00", "Z")
            task.update(
                lease_id=uuid.uuid4().hex,
                lease_expires_at=(now + timedelta(seconds=duration)).isoformat(timespec="seconds").replace("+00:00", "Z"),
                last_heartbeat_at=now_text,
                run_started_at=now_text,
                run_count=int(task["run_count"]) + 1,
                updated_at=now_text,
                revision=int(task["revision"]) + 1,
            )
            return task

        return self._transaction(mutate)

    def heartbeat_task(self, task_id: str, lease_id: str, *, lease_seconds: int = 900) -> dict[str, Any]:
        duration = max(5, min(int(lease_seconds), 3_600))

        def mutate(data: dict[str, Any]) -> dict[str, Any]:
            task = data.setdefault("tasks", {}).get(str(task_id))
            if task is None or task.get("status") != TaskState.RUNNING.value or task.get("lease_id") != lease_id:
                raise TaskLeaseLostError(f"Task '{task_id}' no longer holds its run lease.")
            now = datetime.now(timezone.utc)
            task.update(
                last_heartbeat_at=now.isoformat(timespec="seconds").replace("+00:00", "Z"),
                lease_expires_at=(now + timedelta(seconds=duration)).isoformat(timespec="seconds").replace("+00:00", "Z"),
                updated_at=now.isoformat(timespec="seconds").replace("+00:00", "Z"),
                revision=int(task["revision"]) + 1,
            )
            return task

        return self._transaction(mutate)

    def record_step_complete(
        self,
        task_id: str,
        lease_id: str,
        *,
        step_index: int,
        summary: str,
        action_ids: list[int] | None = None,
    ) -> dict[str, Any]:
        """Advance exactly one step after the runner observed a successful result."""
        clean_summary = _clean(summary, field="step summary", limit=360, required=True)

        def mutate(data: dict[str, Any]) -> dict[str, Any]:
            task = data.setdefault("tasks", {}).get(str(task_id))
            if task is None or task.get("status") != TaskState.RUNNING.value or task.get("lease_id") != lease_id:
                raise TaskLeaseLostError(f"Task '{task_id}' no longer holds its run lease.")
            if int(task["current_step"]) != int(step_index):
                raise TaskConflictError(f"Task '{task_id}' is no longer on step {step_index + 1}.")
            now = utc_now()
            deduped_ids = self._validate_ids(action_ids or [], field="action_ids")
            for action_id in deduped_ids:
                if action_id not in task["related_action_ids"]:
                    task["related_action_ids"].append(action_id)
            task["step_results"].append({
                "step_index": int(step_index),
                "step_id": task["plan"][step_index]["step_id"],
                "status": "completed",
                "summary": clean_summary,
                "completed_at": now,
                "action_ids": deduped_ids,
            })
            task.update(current_step=int(step_index) + 1, updated_at=now, revision=int(task["revision"]) + 1)
            return task

        return self._transaction(mutate)

    def pause_for_confirmation(
        self,
        task_id: str,
        lease_id: str,
        *,
        step_indexes: list[int],
        reason: str,
        action_ids: list[int] | None = None,
    ) -> dict[str, Any]:
        indexes = sorted(set(int(index) for index in step_indexes))
        clean_reason = _clean(reason, field="approval reason", limit=800, required=True)

        def mutate(data: dict[str, Any]) -> dict[str, Any]:
            task = data.setdefault("tasks", {}).get(str(task_id))
            if task is None or task.get("status") != TaskState.RUNNING.value or task.get("lease_id") != lease_id:
                raise TaskLeaseLostError(f"Task '{task_id}' no longer holds its run lease.")
            if not indexes or any(index < int(task["current_step"]) or index >= len(task["plan"]) for index in indexes):
                raise ValueError("Approval request contains invalid task step indexes")
            self._transition(task, TaskState.AWAITING_CONFIRMATION)
            now = utc_now()
            ids = self._validate_ids(action_ids or [], field="action_ids")
            for action_id in ids:
                if action_id not in task["related_action_ids"]:
                    task["related_action_ids"].append(action_id)
            task.update(
                approval_request={
                    "step_indexes": indexes,
                    "reason": clean_reason,
                    "requested_at": now,
                    "plan_fingerprint": self._plan_fingerprint(task["plan"]),
                },
                approved_step_indexes=[],
                lease_id=None,
                lease_expires_at=None,
                last_heartbeat_at=now,
                updated_at=now,
                revision=int(task["revision"]) + 1,
            )
            return task

        return self._transaction(mutate)

    def approve_current_request(self, task_id: str, *, expected_revision: int | None = None) -> dict[str, Any]:
        """Resume only the exact persisted sensitive-step request the user saw."""
        def mutate(data: dict[str, Any]) -> dict[str, Any]:
            task = data.setdefault("tasks", {}).get(str(task_id))
            if task is None:
                raise ValueError(f"Task '{task_id}' was not found")
            if expected_revision is not None and int(task["revision"]) != int(expected_revision):
                raise TaskConflictError(f"Task '{task_id}' changed since revision {expected_revision}.")
            if task["status"] != TaskState.AWAITING_CONFIRMATION.value:
                raise TaskConflictError("Task is not awaiting confirmation.")
            request = task.get("approval_request") or {}
            if request.get("plan_fingerprint") != self._plan_fingerprint(task["plan"]):
                raise TaskConflictError("Task plan changed after the approval prompt; review it again.")
            self._transition(task, TaskState.PENDING)
            task.update(
                approved_step_indexes=list(request.get("step_indexes", [])),
                approval_request=None,
                updated_at=utc_now(),
                revision=int(task["revision"]) + 1,
            )
            return task

        return self._transaction(mutate)

    def complete_task(
        self,
        task_id: str,
        lease_id: str,
        *,
        result_summary: str,
        action_ids: list[int] | None = None,
    ) -> dict[str, Any]:
        summary = _clean(result_summary, field="result_summary", limit=600, required=True)

        def mutate(data: dict[str, Any]) -> dict[str, Any]:
            task = data.setdefault("tasks", {}).get(str(task_id))
            if task is None or task.get("status") != TaskState.RUNNING.value or task.get("lease_id") != lease_id:
                raise TaskLeaseLostError(f"Task '{task_id}' no longer holds its run lease.")
            if int(task["current_step"]) != len(task["plan"]):
                raise TaskConflictError("Task cannot complete before every step is recorded.")
            self._transition(task, TaskState.COMPLETED)
            ids = self._validate_ids(action_ids or [], field="action_ids")
            for action_id in ids:
                if action_id not in task["related_action_ids"]:
                    task["related_action_ids"].append(action_id)
            now = utc_now()
            task.update(
                result_summary=summary,
                lease_id=None,
                lease_expires_at=None,
                last_heartbeat_at=now,
                updated_at=now,
                approved_step_indexes=[],
                revision=int(task["revision"]) + 1,
            )
            return task

        return self._transaction(mutate)

    def fail_task(
        self,
        task_id: str,
        lease_id: str,
        *,
        result_summary: str,
        action_ids: list[int] | None = None,
    ) -> dict[str, Any]:
        summary = _clean(result_summary, field="result_summary", limit=600, required=True)

        def mutate(data: dict[str, Any]) -> dict[str, Any]:
            task = data.setdefault("tasks", {}).get(str(task_id))
            if task is None or task.get("status") != TaskState.RUNNING.value or task.get("lease_id") != lease_id:
                raise TaskLeaseLostError(f"Task '{task_id}' no longer holds its run lease.")
            self._transition(task, TaskState.FAILED)
            ids = self._validate_ids(action_ids or [], field="action_ids")
            for action_id in ids:
                if action_id not in task["related_action_ids"]:
                    task["related_action_ids"].append(action_id)
            now = utc_now()
            task.update(
                result_summary=summary,
                last_error=summary,
                lease_id=None,
                lease_expires_at=None,
                last_heartbeat_at=now,
                updated_at=now,
                approved_step_indexes=[],
                revision=int(task["revision"]) + 1,
            )
            return task

        return self._transaction(mutate)

    def recover_expired_leases(self, now: str | datetime | None = None) -> list[dict[str, Any]]:
        now_dt = _parse_iso(now) if isinstance(now, str) else (now or datetime.now(timezone.utc))
        if now_dt.tzinfo is None:
            now_dt = now_dt.replace(tzinfo=timezone.utc)
        snapshot = self._read()
        if not any(task.get("status") == TaskState.RUNNING.value and self._lease_expired(task, now_dt) for task in snapshot["tasks"].values()):
            return []
        recovered: list[dict[str, Any]] = []

        def mutate(data: dict[str, Any]) -> list[dict[str, Any]]:
            when = now_dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
            for task in data.setdefault("tasks", {}).values():
                if task.get("status") == TaskState.RUNNING.value and self._lease_expired(task, now_dt):
                    self._transition(task, TaskState.FAILED)
                    task.update(
                        result_summary="Previous workflow lease expired; task can be reviewed and retried.",
                        last_error="Workflow lease expired.",
                        lease_id=None,
                        lease_expires_at=None,
                        last_heartbeat_at=when,
                        updated_at=when,
                        approved_step_indexes=[],
                        revision=int(task["revision"]) + 1,
                    )
                    recovered.append(_copy(task))
            return recovered

        return self._transaction(mutate)


class TaskToolHandlers:
    """Safe text/JSON adapters for the public task tool family."""

    def __init__(self, store: TaskStore, session_id_getter: Callable[[], str | None] | None = None):
        self.store = store
        self._session_id_getter = session_id_getter or (lambda: None)

    @staticmethod
    def _json(payload: dict[str, Any]) -> str:
        return json.dumps(payload, ensure_ascii=False, indent=2)

    def create_task(self, args: dict) -> str:
        task = self.store.create_task(
            args.get("goal", ""),
            args.get("plan", []),
            related_person_ids=args.get("related_person_ids") or [],
            session_id=args.get("session_id") or self._session_id_getter(),
        )
        return self._json({"ok": True, "task": task_public_view(task)})

    def list_tasks(self, args: dict | None = None) -> str:
        args = args or {}
        statuses = args.get("statuses") or []
        if isinstance(statuses, str):
            statuses = [statuses]
        tasks = self.store.list_tasks(statuses=statuses, limit=int(args.get("limit", 20)))
        return self._json({"ok": True, "tasks": [task_public_view(task, include_steps=False) for task in tasks]})

    def get_task_status(self, args: dict) -> str:
        task = self.store.get_task(args.get("task_id", ""))
        if task is None:
            return self._json({"ok": False, "error": "Task not found."})
        return self._json({"ok": True, "task": task_public_view(task)})

    def update_task(self, args: dict) -> str:
        task_id = args.get("task_id", "")
        try:
            if bool(args.get("retry", False)):
                task = self.store.retry_task(task_id, expected_revision=args.get("expected_revision"))
            else:
                updates = {
                    key: args[key]
                    for key in ("goal", "plan", "related_person_ids", "session_id")
                    if key in args
                }
                task = self.store.update_task(task_id, expected_revision=args.get("expected_revision"), **updates)
        except (ValueError, TaskConflictError) as exc:
            return self._json({"ok": False, "error": str(exc)})
        return self._json({"ok": True, "task": task_public_view(task)})

    def cancel_task(self, args: dict) -> str:
        try:
            task = self.store.cancel_task(args.get("task_id", ""), expected_revision=args.get("expected_revision"))
        except (ValueError, TaskConflictError) as exc:
            return self._json({"ok": False, "error": str(exc)})
        return self._json({"ok": True, "task": task_public_view(task)})
