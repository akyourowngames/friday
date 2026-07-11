"""Execute durable task plans while preserving every confirmation boundary."""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from copy import deepcopy
from pathlib import Path
from typing import Any

from ares.actions import ActionLedger, action_task_context, collect_action_ids
from ares.tasks import (
    TaskAlreadyRunningError,
    TaskConflictError,
    TaskLeaseLostError,
    TaskStore,
    task_public_view,
)


ExecuteTool = Callable[[str, dict[str, Any]], Awaitable[str]]

_SAFE_LOCAL_TOOLS = {
    "read_file", "search_files", "list_directory", "get_file_info", "glob_pattern",
    "show_file_with_line_numbers", "preview_diff", "find_text", "compare_files",
    "disk_usage", "checksum", "find_duplicates", "tail_file", "head_file",
    "count_lines", "file_tree", "web_search", "fetch_url", "phone_status",
    "phone_get_notifications", "phone_search_contact", "get_current_datetime",
    "image_info", "generate_image", "resize_image", "convert_image", "crop_image",
    "search_memory", "search_person", "search_actions", "list_tasks", "get_task_status",
    "list_cron_jobs", "get_cron_job", "get_cron_logs", "list_skills", "load_skill",
}
_REVERSIBLE_LOCAL_TOOLS = {
    "edit_file", "insert_line", "replace_lines", "delete_lines", "append_to_file",
    "prepend_to_file", "backup_file", "undo_last_edit", "create_directory", "copy_file",
    "create_file_from_template",
}
_ALWAYS_CONFIRM = {
    "delete_file", "phone_call_number", "phone_send_sms", "gmail_send", "gmail_reply",
    "calendar_create_event", "batch_edit", "batch_file_ops", "glob_apply", "run_command",
    "terminal_exec", "run_code", "create_cron_job", "delete_cron_job", "update_cron_job",
    "export_data", "forget_person", "remember_person", "update_person",
}
_MCP_OBSERVE_OR_NAVIGATE = (
    "snapshot", "screenshot", "inspect", "read", "get", "list", "search", "query",
    "wait", "navigate", "goto", "go_to", "scroll", "hover",
)
_MCP_ACTION = ("click", "type", "fill", "press", "submit", "send", "delete", "upload", "download", "purchase", "payment")


def _compact(value: Any, limit: int = 220) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def _mask_recipient(value: Any) -> str:
    raw = _compact(value, 180)
    if "@" in raw:
        local, _, domain = raw.partition("@")
        return f"{local[:1] or '•'}•••@{domain}"
    digits = re.sub(r"\D", "", raw)
    if len(digits) >= 4:
        return f"•••{digits[-4:]}"
    return raw or "recipient"


def _is_mcp(tool_name: str) -> bool:
    return str(tool_name).startswith("mcp__")


def _is_mcp_observation_or_navigation(tool_name: str) -> bool:
    lowered = str(tool_name).casefold()
    return _is_mcp(lowered) and any(token in lowered for token in _MCP_OBSERVE_OR_NAVIGATE) and not any(
        token in lowered for token in _MCP_ACTION
    )


def _is_windows_or_playwright(tool_name: str) -> bool:
    lowered = str(tool_name).casefold()
    return lowered.startswith("mcp__") and ("playwright" in lowered or "windows" in lowered)


def _path_exists(value: Any) -> bool:
    try:
        return Path(str(value)).expanduser().exists()
    except (OSError, ValueError):
        return True


def confirmation_reason(step: dict[str, Any]) -> str | None:
    """Classify a step conservatively; unknown actuation is never auto-approved."""
    tool_name = str(step.get("tool_name") or "")
    lowered = tool_name.casefold()
    args = step.get("arguments") if isinstance(step.get("arguments"), dict) else {}

    if lowered in {"phone_send_sms", "phone_call_number"}:
        return f"{tool_name} to {_mask_recipient(args.get('number'))}"
    if "gmail_send" in lowered or "gmail_reply" in lowered:
        return f"{tool_name} to {_mask_recipient(args.get('to') or args.get('recipient') or 'saved recipient')}"
    if "calendar_create_event" in lowered:
        attendees = args.get("attendees") or []
        if attendees:
            return f"{tool_name} with external attendee(s)"
        return f"{tool_name} changes a calendar"
    if lowered == "delete_file":
        return f"delete file {_compact(args.get('path'), 180)}"
    if lowered in {"batch_edit", "batch_file_ops", "glob_apply"}:
        return f"{tool_name} can change multiple files"
    if lowered == "write_file" and _path_exists(args.get("path")):
        return f"overwrite file {_compact(args.get('path'), 180)}"
    if lowered == "move_file" and _path_exists(args.get("destination")):
        return f"overwrite move destination {_compact(args.get('destination'), 180)}"
    if lowered in _ALWAYS_CONFIRM:
        return f"{tool_name} is consequential"
    if _is_mcp(lowered):
        if _is_mcp_observation_or_navigation(lowered):
            return None
        return f"{tool_name} is an external UI action"
    if lowered in _SAFE_LOCAL_TOOLS or lowered in _REVERSIBLE_LOCAL_TOOLS:
        return None
    # A future tool cannot silently become autonomous just because it was added.
    return f"{tool_name} is not in the autonomous safe allow-list"


def _requires_fresh_mcp_verification(step: dict[str, Any]) -> bool:
    return _is_windows_or_playwright(str(step.get("tool_name") or "")) and not _is_mcp_observation_or_navigation(
        str(step.get("tool_name") or "")
    )


def _is_error_result(result: str) -> bool:
    text = str(result or "").strip()
    lowered = text.casefold()
    if not text or lowered.startswith("error:") or "confirm required" in lowered:
        return True
    try:
        payload = json.loads(text)
    except (TypeError, json.JSONDecodeError):
        payload = None
    if isinstance(payload, dict):
        if payload.get("ok") is False or payload.get("sent") is False or payload.get("dialed") is False:
            return True
        if payload.get("error"):
            return True
    exit_match = re.search(r"^Exit code:\s*(-?\d+)", text, flags=re.MULTILINE)
    return bool(exit_match and int(exit_match.group(1)) != 0)


def _verification_terms(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value if str(item)]
    return [str(value)]


class AutonomousWorkflowRunner:
    """Run an explicit task plan serially through the normal tool execution path."""

    def __init__(
        self,
        *,
        task_store: TaskStore,
        action_ledger: ActionLedger,
        execute_tool: ExecuteTool,
        lease_seconds: int = 900,
    ):
        self.task_store = task_store
        self.action_ledger = action_ledger
        self.execute_tool = execute_tool
        self.lease_seconds = max(30, min(int(lease_seconds), 3_600))

    @staticmethod
    def _result(*, ok: bool, task: dict[str, Any], message: str) -> str:
        return json.dumps({"ok": ok, "message": message, "task": task_public_view(task)}, ensure_ascii=False, indent=2)

    @staticmethod
    def _approval_indexes(task: dict[str, Any]) -> list[int]:
        indexes: list[int] = []
        for index in range(int(task.get("current_step", 0)), len(task.get("plan", []))):
            if confirmation_reason(task["plan"][index]):
                indexes.append(index)
        return indexes

    @staticmethod
    def _approved_arguments(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Satisfy existing confirmation gates only after task approval was persisted."""
        args = deepcopy(arguments)
        lowered = tool_name.casefold()
        if lowered in {"delete_file", "move_file", "write_file", "batch_edit", "glob_apply", "phone_call_number"}:
            args["confirm"] = True
        if lowered in {"insert_line", "replace_lines", "delete_lines", "append_to_file", "prepend_to_file", "batch_file_ops", "create_file_from_template"}:
            args["confirm_dangerous"] = True
        return args

    async def _verify(self, task_id: str, step: dict[str, Any]) -> tuple[bool, str]:
        verify = step.get("verify")
        if _requires_fresh_mcp_verification(step) and not verify:
            return False, "Playwright/Windows action requires a fresh explicit verify snapshot/read-back."
        if not verify:
            return True, ""
        try:
            verification_result = await self.execute_tool(verify["tool_name"], deepcopy(verify.get("arguments") or {}))
        except Exception as exc:  # Tool boundary; detailed output stays out of the ledger.
            return False, f"Verification tool raised {type(exc).__name__}."
        if _is_error_result(verification_result):
            return False, "Verification tool did not report success."
        lowered = verification_result.casefold()
        missing = [term for term in _verification_terms(verify.get("contains")) if term.casefold() not in lowered]
        present = [term for term in _verification_terms(verify.get("not_contains")) if term.casefold() in lowered]
        if missing:
            return False, "Verification result did not contain the expected state."
        if present:
            return False, "Verification result contained a prohibited state."
        self.action_ledger.record(
            "workflow_verification",
            target=verify["tool_name"],
            summary=f"Verified workflow step with {verify['tool_name']}.",
            tool_name=verify["tool_name"],
            task_id=task_id,
            tags=["workflow", "verification"],
        )
        return True, ""

    async def run(self, task_id: str, *, confirm: bool = False, max_steps: int = 25) -> str:
        """Run a task until completion, failure, or a persisted confirmation pause."""
        bounded_steps = max(1, min(int(max_steps), 100))
        self.task_store.recover_expired_leases()
        task = self.task_store.get_task(task_id)
        if task is None:
            return json.dumps({"ok": False, "error": "Task not found."}, ensure_ascii=False, indent=2)
        if task["status"] == "awaiting_confirmation":
            if not confirm:
                return self._result(ok=False, task=task, message="Task is paused pending your explicit approval.")
            try:
                task = self.task_store.approve_current_request(task_id, expected_revision=task.get("revision"))
            except TaskConflictError as exc:
                refreshed = self.task_store.get_task(task_id) or task
                return self._result(ok=False, task=refreshed, message=str(exc))
        if task["status"] != "pending":
            return self._result(ok=False, task=task, message=f"Task cannot run while {task['status']}.")

        try:
            task = self.task_store.claim_task(task_id, lease_seconds=self.lease_seconds)
        except (ValueError, TaskConflictError, TaskAlreadyRunningError) as exc:
            refreshed = self.task_store.get_task(task_id) or task
            return self._result(ok=False, task=refreshed, message=str(exc))

        lease_id = str(task["lease_id"])
        executed = 0
        while int(task["current_step"]) < len(task["plan"]):
            if executed >= bounded_steps:
                with action_task_context(task_id), collect_action_ids() as action_ids:
                    self.action_ledger.record(
                        "workflow_failed",
                        target=task_id,
                        summary="Workflow stopped at its configured execution step limit.",
                        tool_name="run_task",
                        tags=["workflow", "step-limit"],
                    )
                failed = self.task_store.fail_task(
                    task_id, lease_id,
                    result_summary="Workflow stopped at its configured execution step limit; review and retry it.",
                    action_ids=action_ids,
                )
                return self._result(ok=False, task=failed, message="Workflow step limit reached.")

            index = int(task["current_step"])
            step = task["plan"][index]
            reason = confirmation_reason(step)
            approved = index in set(task.get("approved_step_indexes", []))
            if reason and not approved:
                indexes = self._approval_indexes(task)
                summaries = "; ".join(
                    f"step {item + 1}: {confirmation_reason(task['plan'][item])}" for item in indexes
                )
                with action_task_context(task_id), collect_action_ids() as action_ids:
                    self.action_ledger.record(
                        "workflow_confirmation_requested",
                        target=task_id,
                        summary="Workflow paused for explicit confirmation of consequential steps.",
                        tool_name="run_task",
                        tags=["workflow", "confirmation"],
                    )
                paused = self.task_store.pause_for_confirmation(
                    task_id,
                    lease_id,
                    step_indexes=indexes,
                    reason=summaries,
                    action_ids=action_ids,
                )
                return self._result(ok=False, task=paused, message="Workflow paused for confirmation. Review the listed steps and re-run with confirm=true only after approval.")

            arguments = deepcopy(step.get("arguments") or {})
            if reason:
                arguments = self._approved_arguments(step["tool_name"], arguments)
            with action_task_context(task_id), collect_action_ids() as action_ids:
                try:
                    result = await self.execute_tool(step["tool_name"], arguments)
                except Exception as exc:  # Normalized into safe persistent metadata below.
                    result = f"Error: {type(exc).__name__}"
                if _is_error_result(result):
                    self.action_ledger.record(
                        "workflow_step_failed",
                        target=step["tool_name"],
                        summary=f"Workflow step {index + 1} failed while running {step['tool_name']}.",
                        tool_name=step["tool_name"],
                        tags=["workflow", "failed"],
                    )
                    failed = self.task_store.fail_task(
                        task_id,
                        lease_id,
                        result_summary=f"Step {index + 1} ({step['tool_name']}) failed; review and retry the task.",
                        action_ids=action_ids,
                    )
                    return self._result(ok=False, task=failed, message=f"Workflow failed at step {index + 1}.")
                verified, verification_error = await self._verify(task_id, step)
                if not verified:
                    self.action_ledger.record(
                        "workflow_step_failed",
                        target=step["tool_name"],
                        summary=f"Workflow step {index + 1} did not pass its verification gate.",
                        tool_name=step["tool_name"],
                        tags=["workflow", "verification-failed"],
                    )
                    failed = self.task_store.fail_task(
                        task_id,
                        lease_id,
                        result_summary=f"Step {index + 1} ({step['tool_name']}) verification failed: {verification_error}",
                        action_ids=action_ids,
                    )
                    return self._result(ok=False, task=failed, message=verification_error)
                self.action_ledger.record(
                    "workflow_step_completed",
                    target=step["tool_name"],
                    summary=f"Completed workflow step {index + 1} with {step['tool_name']}.",
                    tool_name=step["tool_name"],
                    tags=["workflow", "step"],
                )

            try:
                task = self.task_store.record_step_complete(
                    task_id,
                    lease_id,
                    step_index=index,
                    summary=f"Completed step {index + 1} with {step['tool_name']}.",
                    action_ids=action_ids,
                )
                task = self.task_store.heartbeat_task(task_id, lease_id, lease_seconds=self.lease_seconds)
            except (TaskConflictError, TaskLeaseLostError) as exc:
                refreshed = self.task_store.get_task(task_id) or task
                return self._result(ok=False, task=refreshed, message=str(exc))
            executed += 1

        with action_task_context(task_id), collect_action_ids() as completion_ids:
            self.action_ledger.record(
                "workflow_completed",
                target=task_id,
                summary="Completed all workflow steps.",
                tool_name="run_task",
                tags=["workflow", "completed"],
            )
        completed = self.task_store.complete_task(
            task_id,
            lease_id,
            result_summary="Completed all planned workflow steps.",
            action_ids=completion_ids,
        )
        return self._result(ok=True, task=completed, message="Workflow completed.")
