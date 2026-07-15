"""Tool visibility, authorization, and concurrency policy for Ares agents."""

from __future__ import annotations

import os
import hashlib
import json
import re
import threading
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any, Iterable, Sequence

from ares.multi_agent import AgentCapability, AgentSpec


class ToolResource(str, Enum):
    READ_ONLY = "read_only"
    FILESYSTEM_READ = "filesystem_read"
    FILESYSTEM_WRITE = "filesystem_write"
    BROWSER_READ = "browser_read"
    # Compatibility alias used by the first native runtime release.
    BROWSER_SHARED = "browser_read"
    BROWSER_INTERACTION = "browser_interaction"
    SHELL_SHARED = "shell_shared"
    REPL_SHARED = "repl_shared"
    PROJECT_CHECK = "project_check"
    COMMUNICATION = "communication"
    DATABASE_READ = "database_read"
    DATABASE_WRITE = "database_write"
    EXTERNAL_MUTATION = "external_mutation"
    DELEGATION = "delegation"


DELEGATION_TOOLS = frozenset({
    "list_agents", "delegate_task", "delegate_tasks_parallel", "get_agent_run",
    "list_agent_runs", "get_latest_agent_run", "cancel_agent_run", "resume_agent_run",
})
FILESYSTEM_READ_TOOLS = frozenset({
    "read_file", "search_files", "list_directory", "get_file_info", "glob_pattern",
    "show_file_with_line_numbers", "find_text", "compare_files", "head_file", "tail_file",
    "file_tree", "checksum", "disk_usage", "find_duplicates", "preview_diff",
    "safe_path_status", "count_lines", "image_info",
})
READ_ONLY_LOCAL_TOOLS = frozenset({
    "web_search", "fetch_url", "list_skills", "load_skill", "get_current_datetime",
})
FILESYSTEM_WRITE_TOOLS = frozenset({
    "write_file", "edit_file", "create_directory", "delete_file", "move_file", "copy_file",
    "batch_edit", "batch_file_ops", "glob_apply", "insert_line", "replace_lines", "delete_lines",
    "backup_file", "undo_last_edit", "append_to_file", "prepend_to_file", "create_file_from_template",
    "download_online_file", "extract_document", "create_research_report", "export_data",
    "generate_image", "resize_image", "convert_image", "crop_image", "create_skill",
})
SHELL_TOOLS = frozenset({"run_command", "terminal_exec"})
REPL_TOOLS = frozenset({"run_python", "run_code"})
PROJECT_CHECK_TOOLS = frozenset({"run_project_check"})
COMMUNICATION_TOOLS = frozenset({
    "send_email", "telegram_send_file", "telegram_send_message", "phone_send_sms", "phone_call_number",
    "telephony_call", "telephony_answer", "telephony_hangup", "telephony_mute",
    "telephony_transfer", "mcp__gmail__gmail_send", "mcp__telegram__send_message",
})
DATABASE_READ_TOOLS = frozenset({
    "search_memory", "search_actions", "search_person", "list_tasks", "get_task_status",
    "list_goals", "get_goal_status", "get_goal_signals", "list_watchers", "get_watcher",
    "list_follow_ups",
    "list_watcher_events", "get_watcher_overview", "get_cron_job", "list_cron_jobs",
    "get_cron_logs", "telephony_get_call", "telephony_list_calls", "telephony_list_contacts",
})
EXTERNAL_MUTATION_TOOLS = frozenset({
    "phone_launch_app", "phone_open_url", "install_marketplace_skill", "add_marketplace_mcp",
    "update_config", "run_watcher_now",
})
MULTI_EFFECT_CAPABILITIES: dict[str, tuple[AgentCapability, ...]] = {
    "install_marketplace_skill": (
        AgentCapability.EXTERNAL_MUTATION,
        AgentCapability.FILESYSTEM_WRITE,
    ),
    "add_marketplace_mcp": (
        AgentCapability.EXTERNAL_MUTATION,
        AgentCapability.FILESYSTEM_WRITE,
    ),
    "update_config": (
        AgentCapability.EXTERNAL_MUTATION,
        AgentCapability.FILESYSTEM_WRITE,
    ),
}
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
    "npm publish", "twine upload", "docker push", "remove-item",
    " del ", " rmdir ", " rm ", "shutdown", "format ", "curl -x post", "invoke-webrequest -method post",
)
CHILD_FORBIDDEN_GIT_HINTS = ("git push", "gh pr create")
SHELL_INDIRECT_EXECUTION = re.compile(
    r"(?:^|[;&|]\s*|\s)(?:python(?:3|\.exe)?|py|powershell|pwsh|cmd|bash|sh)\s+(?:-[a-z]*c\b|/c\b|-command\b|-encodedcommand\b)",
    re.IGNORECASE,
)
SHELL_EXTERNAL_MUTATION = re.compile(
    r"(?:git\s+push\b|gh\s+pr\s+create\b|npm\s+publish\b|pnpm\s+publish\b|yarn\s+npm\s+publish\b|"
    r"twine\s+upload\b|docker\s+push\b|curl\b[^\r\n]*(?:\s-X\s*(?:POST|PUT|PATCH|DELETE)\b|\s--data|\s-d\s)|"
    r"(?:invoke-webrequest|invoke-restmethod|iwr|irm)\b[^\r\n]*-method\s*(?:POST|PUT|PATCH|DELETE)\b)",
    re.IGNORECASE,
)
CODE_EXTERNAL_MUTATION = re.compile(
    r"(?:requests\.(?:post|put|patch|delete)\s*\(|smtplib\.|subprocess\.|os\.system\s*\(|"
    r"urllib\.request\.(?:Request|urlopen)\s*\([^\n]*(?:data|method\s*=)|git\s+push|npm\s+publish)",
    re.IGNORECASE,
)
PATH_ARGUMENTS = ("path", "file_path", "target", "destination", "output_path", "source", "cwd")

BROWSER_READ_HINTS = (
    "snapshot", "screenshot", "get_content", "get_text", "inspect", "console_messages",
    "network_requests", "tabs", "list_pages", "read", "pdf",
)
BROWSER_INTERACTION_HINTS = (
    "click", "type", "fill", "upload", "submit", "navigate", "goto", "open_url",
    "press", "select", "drag", "hover", "check", "uncheck", "close", "back", "forward",
)
MCP_READ_VERBS = frozenset({
    "get", "list", "read", "search", "find", "fetch", "query", "inspect",
    "snapshot", "screenshot", "lookup", "retrieve", "status", "describe",
})


def _utcnow() -> datetime:
    return datetime.now(UTC)


def normalize_tool_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    """Canonicalize arguments for exact, replay-resistant action grants."""
    ignored = CONFIRMATION_KEYS | frozenset({"action_grant_id", "grant_id"})

    def normalize(value: Any, key: str = "") -> Any:
        if isinstance(value, dict):
            return {
                str(child_key): normalize(child, str(child_key))
                for child_key, child in sorted(value.items(), key=lambda item: str(item[0]))
                if str(child_key) not in ignored
            }
        if isinstance(value, (list, tuple)):
            return [normalize(item, key) for item in value]
        if key in PATH_ARGUMENTS and isinstance(value, (str, os.PathLike)):
            return _normal_path(value) or str(value)
        return value

    return normalize(dict(arguments))


def action_argument_hash(tool: str, arguments: dict[str, Any]) -> str:
    payload = json.dumps(
        {"tool": str(tool), "arguments": normalize_tool_arguments(arguments)},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ActionGrant:
    grant_id: str
    root_run_id: str
    child_run_id: str
    tool: str
    argument_hash: str
    request_id: str
    expires_at: str
    confirmed: bool = True


class ActionGrantRegistry:
    """Root-owned, in-memory single-use action grants.

    Children receive only opaque IDs.  They cannot create, rewrite, extend, or
    reuse a grant, and any argument change invalidates the authorization.
    """

    def __init__(self) -> None:
        self._grants: dict[str, ActionGrant] = {}
        self._used: set[str] = set()
        self._lock = threading.RLock()

    def issue(
        self,
        *,
        root_run_id: str,
        child_run_id: str,
        tool: str,
        arguments: dict[str, Any],
        request_id: str,
        ttl_seconds: float = 300.0,
        explicit_user_confirmation: bool,
    ) -> ActionGrant:
        if not explicit_user_confirmation:
            raise PermissionError("an action grant requires explicit user confirmation")
        if not all(str(value).strip() for value in (root_run_id, child_run_id, tool, request_id)):
            raise ValueError("root run, child run, tool, and request id are required")
        expires = _utcnow() + timedelta(seconds=max(1.0, min(float(ttl_seconds), 3600.0)))
        grant = ActionGrant(
            grant_id=f"grant_{uuid.uuid4().hex}",
            root_run_id=str(root_run_id),
            child_run_id=str(child_run_id),
            tool=str(tool),
            argument_hash=action_argument_hash(tool, arguments),
            request_id=str(request_id),
            expires_at=expires.isoformat(),
        )
        with self._lock:
            self._grants[grant.grant_id] = grant
        return grant

    def consume(
        self,
        grant_id: str,
        *,
        root_run_id: str,
        child_run_id: str,
        tool: str,
        arguments: dict[str, Any],
        request_id: str,
    ) -> ToolAccessDecision:
        with self._lock:
            grant = self._grants.get(str(grant_id))
            if grant is None:
                return ToolAccessDecision(False, "action grant not found")
            if grant.grant_id in self._used:
                return ToolAccessDecision(False, "action grant has already been used")
            try:
                expires_at = datetime.fromisoformat(grant.expires_at)
            except ValueError:
                return ToolAccessDecision(False, "action grant expiration is invalid")
            if expires_at <= _utcnow():
                return ToolAccessDecision(False, "action grant has expired")
            expected = (
                grant.root_run_id == str(root_run_id)
                and grant.child_run_id == str(child_run_id)
                and grant.tool == str(tool)
                and grant.request_id == str(request_id)
                and grant.argument_hash == action_argument_hash(tool, arguments)
                and grant.confirmed
            )
            if not expected:
                return ToolAccessDecision(False, "action grant scope or arguments do not match")
            self._used.add(grant.grant_id)
            return ToolAccessDecision(True)

    def revoke_root(self, root_run_id: str) -> None:
        with self._lock:
            for grant_id, grant in tuple(self._grants.items()):
                if grant.root_run_id == root_run_id:
                    self._grants.pop(grant_id, None)
                    self._used.discard(grant_id)


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
    if name in PROJECT_CHECK_TOOLS:
        return ToolResource.PROJECT_CHECK
    if name in COMMUNICATION_TOOLS or any(hint in lowered for hint in ("gmail_send", "telegram_send", "send_sms")):
        return ToolResource.COMMUNICATION
    if name in DATABASE_READ_TOOLS:
        return ToolResource.DATABASE_READ
    if name in READ_ONLY_LOCAL_TOOLS:
        return ToolResource.READ_ONLY
    if lowered.startswith("mcp__playwright__") or lowered.startswith("mcp__windows__") or "browser_" in lowered:
        if any(hint in lowered for hint in BROWSER_INTERACTION_HINTS):
            return ToolResource.BROWSER_INTERACTION
        if any(hint in lowered for hint in BROWSER_READ_HINTS):
            return ToolResource.BROWSER_READ
        # Unknown browser tools are interaction-capable by default.
        return ToolResource.BROWSER_INTERACTION
    if lowered.startswith(DATABASE_WRITE_PREFIXES):
        return ToolResource.DATABASE_WRITE
    if name in EXTERNAL_MUTATION_TOOLS:
        return ToolResource.EXTERNAL_MUTATION
    if lowered.startswith("mcp__"):
        if any(hint in lowered for hint in EXTERNAL_MUTATION_HINTS):
            return ToolResource.EXTERNAL_MUTATION
        operation = lowered.rsplit("__", 1)[-1]
        operation_tokens = frozenset(token for token in re.split(r"[^a-z0-9]+", operation) if token)
        if operation_tokens & MCP_READ_VERBS:
            return ToolResource.READ_ONLY
        # Connected tools are an external authority boundary. Unknown verbs
        # fail closed instead of silently becoming read-only.
        return ToolResource.EXTERNAL_MUTATION
    # Unknown local/plugin tools also fail closed. New tools must be explicitly
    # categorized before a child role can receive them.
    return ToolResource.EXTERNAL_MUTATION


def required_capability(name: str) -> AgentCapability | None:
    resource = classify_tool(name)
    return {
        ToolResource.FILESYSTEM_READ: AgentCapability.FILESYSTEM_READ,
        ToolResource.FILESYSTEM_WRITE: AgentCapability.FILESYSTEM_WRITE,
        ToolResource.SHELL_SHARED: AgentCapability.SHELL_EXECUTION,
        ToolResource.REPL_SHARED: AgentCapability.CODE_EXECUTION,
        ToolResource.PROJECT_CHECK: AgentCapability.SHELL_EXECUTION,
        ToolResource.BROWSER_READ: AgentCapability.BROWSER_READ,
        ToolResource.BROWSER_INTERACTION: AgentCapability.BROWSER_INTERACTION,
        ToolResource.COMMUNICATION: AgentCapability.COMMUNICATION,
        ToolResource.DATABASE_READ: AgentCapability.DATABASE_READ,
        ToolResource.DATABASE_WRITE: AgentCapability.DATABASE_WRITE,
        ToolResource.EXTERNAL_MUTATION: AgentCapability.EXTERNAL_MUTATION,
        ToolResource.DELEGATION: AgentCapability.DELEGATION,
    }.get(resource)


def required_capabilities(name: str) -> tuple[AgentCapability, ...]:
    explicit = MULTI_EFFECT_CAPABILITIES.get(name)
    if explicit is not None:
        return explicit
    capability = required_capability(name)
    return (capability,) if capability is not None else ()


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
        capabilities = required_capabilities(name)
        if any(not spec.permits_capability(capability) for capability in capabilities):
            continue
        # Shells and general-purpose REPLs cannot be made reliably read-only:
        # either can write files through standard-library or builtin commands.
        # Requiring filesystem-write authority closes the indirect mutation
        # path for custom roles that were granted execution but not mutation.
        if resource in {ToolResource.SHELL_SHARED, ToolResource.REPL_SHARED} and not spec.permits_capability(
            AgentCapability.FILESYSTEM_WRITE
        ):
            continue
        if resource in {ToolResource.SHELL_SHARED, ToolResource.REPL_SHARED} and not spec.permits_capability(
            AgentCapability.EXTERNAL_MUTATION
        ):
            # General interpreters cannot be reliably confined to a workspace
            # without an OS sandbox. Built-in children therefore never see
            # them; an explicitly configured operator still needs a per-call
            # exact action grant at dispatch.
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
    grant_registry: ActionGrantRegistry | None = None,
    root_run_id: str = "",
    child_run_id: str = "",
    request_id: str = "",
    workspace_root: str = "",
) -> ToolAccessDecision:
    if not matches_allowlist(name, spec.allowed_tools):
        return ToolAccessDecision(False, f"tool {name!r} is outside the {spec.name} allowlist")
    resource = classify_tool(name)
    if resource is ToolResource.DELEGATION and not (allow_delegation and spec.can_delegate):
        return ToolAccessDecision(False, "recursive delegation is not authorized")
    capabilities = required_capabilities(name)
    missing_capabilities = [
        capability for capability in capabilities if not spec.permits_capability(capability)
    ]
    if missing_capabilities:
        return ToolAccessDecision(
            False,
            f"{spec.name} lacks {', '.join(capability.value for capability in missing_capabilities)} capability",
        )
    if resource in {ToolResource.SHELL_SHARED, ToolResource.REPL_SHARED} and not spec.permits_capability(
        AgentCapability.FILESYSTEM_WRITE
    ):
        return ToolAccessDecision(
            False,
            f"{spec.name} lacks filesystem_write capability required for mutation-capable shell/code execution",
        )
    if resource in {ToolResource.SHELL_SHARED, ToolResource.REPL_SHARED} and not spec.permits_capability(
        AgentCapability.EXTERNAL_MUTATION
    ):
        return ToolAccessDecision(
            False,
            "general child shell/code execution requires external_mutation capability and an exact action grant",
        )
    if child_agent and any(bool(arguments.get(key)) for key in CONFIRMATION_KEYS):
        return ToolAccessDecision(False, "a child agent cannot originate user confirmation")
    if child_agent and workspace_root and resource in {
        ToolResource.FILESYSTEM_READ,
        ToolResource.FILESYSTEM_WRITE,
        ToolResource.SHELL_SHARED,
        ToolResource.REPL_SHARED,
        ToolResource.PROJECT_CHECK,
    }:
        root = Path(workspace_root).expanduser().resolve(strict=False)
        raw_paths = [arguments.get(key) for key in PATH_ARGUMENTS if arguments.get(key)]
        if resource in {ToolResource.SHELL_SHARED, ToolResource.REPL_SHARED, ToolResource.PROJECT_CHECK}:
            raw_paths = [arguments.get("cwd")]
            if not raw_paths[0]:
                return ToolAccessDecision(
                    False, "child execution/check calls require an explicit workspace cwd"
                )
        if resource in {ToolResource.FILESYSTEM_READ, ToolResource.FILESYSTEM_WRITE} and not raw_paths:
            return ToolAccessDecision(False, "child filesystem access requires an explicit workspace path")
        for raw_path in raw_paths:
            candidate = Path(str(raw_path)).expanduser()
            if not candidate.is_absolute():
                candidate = root / candidate
            try:
                candidate.resolve(strict=False).relative_to(root)
            except (OSError, ValueError):
                return ToolAccessDecision(False, "child mutation path is outside its assigned workspace")
    needs_action_grant = child_agent and resource in {
        ToolResource.BROWSER_INTERACTION,
        ToolResource.COMMUNICATION,
        ToolResource.EXTERNAL_MUTATION,
        ToolResource.SHELL_SHARED,
        ToolResource.REPL_SHARED,
    }
    if child_agent and resource is ToolResource.SHELL_SHARED:
        command = f" {str(arguments.get('command') or arguments.get('code') or '').casefold()} "
        if any(hint in command for hint in CHILD_FORBIDDEN_GIT_HINTS):
            return ToolAccessDecision(False, "child agents cannot push Git changes or create pull requests")
        if any(hint in command for hint in CHILD_SHELL_DENY_HINTS):
            needs_action_grant = True
        if SHELL_INDIRECT_EXECUTION.search(command):
            return ToolAccessDecision(False, "opaque nested interpreters are not allowed in child shell calls")
        if workspace_root and re.search(r"(?:^|[;&|]\s*|\s)(?:cd|chdir|pushd|popd|set-location)\b|(?:^|[\\/])\.\.(?:[\\/]|$)", command):
            return ToolAccessDecision(False, "child shell calls cannot change or traverse outside their workspace")
        if SHELL_EXTERNAL_MUTATION.search(command):
            needs_action_grant = True
    if child_agent and resource is ToolResource.REPL_SHARED:
        code = str(arguments.get("code") or "")
        if CODE_EXTERNAL_MUTATION.search(code):
            needs_action_grant = True
    if needs_action_grant:
        if not spec.permits_capability(AgentCapability.EXTERNAL_MUTATION) and resource not in {
            ToolResource.BROWSER_INTERACTION,
            ToolResource.COMMUNICATION,
        }:
            return ToolAccessDecision(False, "consequential indirect action requires external_mutation capability")
        grant_id = str(arguments.get("action_grant_id") or arguments.get("grant_id") or "")
        if grant_registry is None or not grant_id:
            return ToolAccessDecision(False, "consequential child action requires an exact single-use action grant")
        decision = grant_registry.consume(
            grant_id,
            root_run_id=root_run_id,
            child_run_id=child_run_id,
            tool=name,
            arguments=arguments,
            request_id=request_id,
        )
        if not decision.allowed:
            return decision
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
        ToolResource.BROWSER_READ,
        ToolResource.BROWSER_INTERACTION,
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
        if not left.paths or not right.paths:
            return False
        return not paths_overlap(left.paths, right.paths)
    return True


def execution_waves(resources: Sequence[ToolCallResource]) -> tuple[tuple[int, ...], ...]:
    """Build adjacent, order-preserving waves with mutation barriers.

    A later read is never moved into an earlier wave across an intervening
    conflicting mutation.  Independent adjacent operations still overlap.
    """
    waves: list[list[ToolCallResource]] = []
    current: list[ToolCallResource] = []
    for resource in resources:
        if current and not all(can_run_together(resource, existing) for existing in current):
            waves.append(current)
            current = []
        current.append(resource)
    if current:
        waves.append(current)
    return tuple(tuple(item.index for item in wave) for wave in waves)
