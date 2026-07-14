"""Tool visibility, authorization, and concurrency policy for Ares agents."""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any, Iterable, Sequence

from ares.multi_agent import AgentSpec


class ToolResource(str, Enum):
    READ_ONLY = "read_only"
    FILESYSTEM_READ = "filesystem_read"
    FILESYSTEM_WRITE = "filesystem_write"
    BROWSER_SHARED = "browser_shared"
    SHELL_SHARED = "shell_shared"
    REPL_SHARED = "repl_shared"
    COMMUNICATION = "communication"
    DATABASE_WRITE = "database_write"
    EXTERNAL_MUTATION = "external_mutation"
    DELEGATION = "delegation"


DELEGATION_TOOLS = frozenset({
    "list_agents", "delegate_task", "delegate_tasks_parallel", "get_agent_run", "cancel_agent_run",
})
FILESYSTEM_READ_TOOLS = frozenset({
    "read_file", "search_files", "list_directory", "get_file_info", "glob_pattern",
    "show_file_with_line_numbers", "find_text", "compare_files", "head_file", "tail_file",
    "file_tree", "checksum", "disk_usage", "find_duplicates",
})
FILESYSTEM_WRITE_TOOLS = frozenset({
    "write_file", "edit_file", "create_directory", "delete_file", "move_file", "copy_file",
    "batch_edit", "batch_file_ops", "glob_apply", "insert_line", "replace_lines", "delete_lines",
    "backup_file", "undo_last_edit", "append_to_file", "prepend_to_file", "create_file_from_template",
})
SHELL_TOOLS = frozenset({"run_command", "terminal_exec"})
REPL_TOOLS = frozenset({"run_python", "run_code"})
COMMUNICATION_TOOLS = frozenset({
    "send_email", "telegram_send_file", "telegram_send_message", "phone_send_sms", "phone_call_number",
    "telephony_call", "telephony_hangup", "mcp__gmail__gmail_send", "mcp__telegram__send_message",
})
DATABASE_WRITE_PREFIXES = (
    "store_", "update_memory", "delete_memory", "remember_person", "update_person", "forget_person",
    "create_goal", "update_goal", "delete_goal", "create_task", "update_task", "create_cron_",
    "update_cron_", "delete_cron_", "create_watcher", "update_watcher", "remove_watcher",
)
EXTERNAL_MUTATION_HINTS = (
    "send", "create_event", "delete", "publish", "push", "call", "hangup", "launch_app", "open_url",
)
CONFIRMATION_KEYS = frozenset({"confirm", "confirmed", "confirm_dangerous", "user_confirmed"})
CHILD_SHELL_DENY_HINTS = (
    "git push", "gh pr create", "npm publish", "twine upload", "docker push", "remove-item",
    " del ", " rmdir ", " rm ", "shutdown", "format ", "curl -x post", "invoke-webrequest -method post",
)
PATH_ARGUMENTS = ("path", "file_path", "target", "destination", "output_path", "source", "cwd")


@dataclass(frozen=True, slots=True)
class ToolAccessDecision:
    allowed: bool
    reason: str = ""


@dataclass(frozen=True, slots=True)
class ToolCallResource:
    index: int
    name: str
    resource: ToolResource
    paths: tuple[str, ...] = ()


def tool_name(schema: dict[str, Any]) -> str:
    return str(schema.get("function", {}).get("name") or "")


def matches_allowlist(name: str, patterns: Iterable[str]) -> bool:
    return any(fnmatchcase(name, pattern) for pattern in patterns)


def classify_tool(name: str) -> ToolResource:
    lowered = name.casefold()
    if name in DELEGATION_TOOLS:
        return ToolResource.DELEGATION
    if name in FILESYSTEM_READ_TOOLS:
        return ToolResource.FILESYSTEM_READ
    if name in FILESYSTEM_WRITE_TOOLS:
        return ToolResource.FILESYSTEM_WRITE
    if name in SHELL_TOOLS:
        return ToolResource.SHELL_SHARED
    if name in REPL_TOOLS:
        return ToolResource.REPL_SHARED
    if name in COMMUNICATION_TOOLS or any(hint in lowered for hint in ("gmail_send", "telegram_send", "send_sms")):
        return ToolResource.COMMUNICATION
    if lowered.startswith("mcp__playwright__") or lowered.startswith("mcp__windows__") or "browser_" in lowered:
        return ToolResource.BROWSER_SHARED
    if lowered.startswith(DATABASE_WRITE_PREFIXES):
        return ToolResource.DATABASE_WRITE
    if lowered.startswith("mcp__") and any(hint in lowered for hint in EXTERNAL_MUTATION_HINTS):
        return ToolResource.EXTERNAL_MUTATION
    return ToolResource.READ_ONLY


def filter_tool_schemas(
    schemas: Sequence[dict[str, Any]],
    spec: AgentSpec,
    *,
    allow_delegation: bool = False,
) -> list[dict[str, Any]]:
    """Filter schemas before an LLM sees them; runtime checks remain mandatory."""
    visible: list[dict[str, Any]] = []
    for schema in schemas:
        name = tool_name(schema)
        if not name or not matches_allowlist(name, spec.allowed_tools):
            continue
        resource = classify_tool(name)
        if resource is ToolResource.DELEGATION and not (allow_delegation and spec.can_delegate):
            continue
        if not spec.can_mutate and resource in {
            ToolResource.FILESYSTEM_WRITE,
            ToolResource.SHELL_SHARED,
            ToolResource.REPL_SHARED,
            ToolResource.COMMUNICATION,
            ToolResource.DATABASE_WRITE,
            ToolResource.EXTERNAL_MUTATION,
        }:
            continue
        visible.append(schema)
    return visible


def authorize_tool_call(
    spec: AgentSpec,
    name: str,
    arguments: dict[str, Any],
    *,
    allow_delegation: bool = False,
    child_agent: bool = True,
) -> ToolAccessDecision:
    if not matches_allowlist(name, spec.allowed_tools):
        return ToolAccessDecision(False, f"tool {name!r} is outside the {spec.name} allowlist")
    resource = classify_tool(name)
    if resource is ToolResource.DELEGATION and not (allow_delegation and spec.can_delegate):
        return ToolAccessDecision(False, "recursive delegation is not authorized")
    if not spec.can_mutate and resource not in {
        ToolResource.READ_ONLY, ToolResource.FILESYSTEM_READ, ToolResource.BROWSER_SHARED,
    }:
        return ToolAccessDecision(False, f"{spec.name} is read-only")
    if child_agent and any(bool(arguments.get(key)) for key in CONFIRMATION_KEYS):
        return ToolAccessDecision(False, "a child agent cannot originate user confirmation")
    if child_agent and resource is ToolResource.SHELL_SHARED:
        command = f" {str(arguments.get('command') or arguments.get('code') or '').casefold()} "
        if any(hint in command for hint in CHILD_SHELL_DENY_HINTS):
            return ToolAccessDecision(False, "consequential shell actions require the root user-facing confirmation flow")
    return ToolAccessDecision(True)


def _normal_path(value: Any) -> str | None:
    if not isinstance(value, (str, os.PathLike)) or not str(value).strip():
        return None
    try:
        return os.path.normcase(str(Path(value).expanduser().resolve(strict=False)))
    except OSError:
        return os.path.normcase(os.path.abspath(os.path.expanduser(str(value))))


def call_resource(index: int, call: dict[str, Any], arguments: dict[str, Any]) -> ToolCallResource:
    name = str(call.get("function", {}).get("name") or "unknown")
    paths = tuple(filter(None, (_normal_path(arguments.get(key)) for key in PATH_ARGUMENTS)))
    return ToolCallResource(index=index, name=name, resource=classify_tool(name), paths=paths)


def paths_overlap(left: Sequence[str], right: Sequence[str]) -> bool:
    for first in left:
        for second in right:
            try:
                common = os.path.commonpath([first, second])
            except ValueError:
                continue
            if common in {first, second}:
                return True
    return False


def can_run_together(left: ToolCallResource, right: ToolCallResource) -> bool:
    serial = {
        ToolResource.BROWSER_SHARED,
        ToolResource.SHELL_SHARED,
        ToolResource.REPL_SHARED,
        ToolResource.COMMUNICATION,
        ToolResource.DATABASE_WRITE,
        ToolResource.EXTERNAL_MUTATION,
        ToolResource.DELEGATION,
    }
    if left.resource in serial or right.resource in serial:
        return False
    if ToolResource.FILESYSTEM_WRITE in {left.resource, right.resource}:
        return not paths_overlap(left.paths, right.paths)
    return True


def execution_waves(resources: Sequence[ToolCallResource]) -> tuple[tuple[int, ...], ...]:
    """Greedily group safe independent calls while preserving model order."""
    waves: list[list[ToolCallResource]] = []
    for resource in resources:
        placed = False
        for wave in waves:
            if all(can_run_together(resource, existing) for existing in wave):
                wave.append(resource)
                placed = True
                break
        if not placed:
            waves.append([resource])
    return tuple(tuple(item.index for item in wave) for wave in waves)
