"""Categorized, current-turn-aware root tool selection.

The registry only controls which schemas are advertised to the root model.  It
is deliberately not an authorization boundary: callers must still apply the
runtime checks in :mod:`ares.turn_policy` immediately before execution.
"""

from __future__ import annotations

import copy
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any


class ToolCategory(str, Enum):
    CORE_CONVERSATION = "core_conversation"
    SKILLS = "skills"
    RECALL = "recall"
    DELEGATION = "delegation"
    WORKFLOWS = "workflows"
    FILES = "files"
    CODE_EXECUTION = "code_execution"
    BROWSER_DOM = "browser_dom"
    # Compatibility alias for callers from before browser and desktop
    # automation were split into separate surfaces.
    BROWSER = "browser_dom"
    DESKTOP_UIA = "desktop_uia"
    SYSTEM = "system"
    RESEARCH = "research"
    COMMUNICATION = "communication"
    GOALS = "goals"
    WATCHERS = "watchers"
    VISION = "vision"
    PHONE = "phone"
    TELEPHONY = "telephony"
    MCP = "mcp"
    UNKNOWN_CONSEQUENTIAL = "unknown_consequential"


DELEGATION_TOOL_NAMES = frozenset({
    "list_agents",
    "delegate_task",
    "delegate_tasks_parallel",
    "get_agent_run",
    "list_agent_runs",
    "get_latest_agent_run",
    "cancel_agent_run",
    "resume_agent_run",
})
AGENT_INTROSPECTION_TOOL_NAMES = frozenset({
    "list_agents", "get_agent_run", "list_agent_runs", "get_latest_agent_run",
})
WORKFLOW_TOOL_NAMES = frozenset({
    "create_task", "list_tasks", "get_task_status", "update_task", "cancel_task", "run_task",
    "create_workflow_task", "run_workflow_task",
})
RECALL_TOOL_NAMES = frozenset({
    "search_memory", "search_actions", "search_person", "list_memories", "get_memory",
    "list_learning_reviews",
})
READ_ONLY_FILE_TOOL_NAMES = frozenset({
    "read_file", "search_files", "list_directory", "get_file_info", "glob_pattern",
    "show_file_with_line_numbers", "find_text", "compare_files", "head_file", "tail_file",
    "file_tree", "checksum", "disk_usage", "find_duplicates", "preview_diff", "image_info",
})
FILE_TOOL_NAMES = READ_ONLY_FILE_TOOL_NAMES | frozenset({
    "write_file", "edit_file",
    "create_directory", "delete_file", "move_file", "copy_file", "batch_edit",
    "batch_file_ops", "glob_apply", "insert_line", "replace_lines", "delete_lines",
    "backup_file", "undo_last_edit", "append_to_file", "prepend_to_file",
    "create_file_from_template", "generate_image", "generate_chart", "analyze_data", "convert_document", "resize_image", "convert_image", "crop_image",
})
CODE_TOOL_NAMES = frozenset({"run_command", "terminal_exec", "run_python", "run_code"})
RESEARCH_TOOL_NAMES = frozenset({
    "web_search", "fetch_url", "download_online_file", "extract_document",
    "create_research_report",
})
COMMUNICATION_TOOL_NAMES = frozenset({
    "send_email", "telegram_send_file", "telegram_send_message",
})
CORE_TOOL_NAMES = frozenset({"get_current_datetime"})
SYSTEM_MUTATION_TOOL_NAMES = frozenset({"update_config"})
SKILL_ROUTING_TOOL_NAMES = frozenset({
    "list_skills", "load_skill", "search_skill_marketplace",
})
SKILL_MUTATION_TOOL_NAMES = frozenset({
    "create_skill", "install_marketplace_skill", "uninstall_skill",
})
VISION_READ_TOOL_NAMES = frozenset({
    "vision_compare", "vision_list_watches",
    "vision_list_events", "vision_list_sources",
})
VISION_LIVE_TOOL_NAMES = frozenset({
    "vision_observe", "vision_start_source",
})
_BROWSER_SESSION_TURN_RE = re.compile(
    r"\b(?:new|fresh)\s+(?:playwright|browser|chrome)(?:\s+session+)?\b|"
    r"\b(?:start|launch)\s+(?:a\s+)?(?:new|fresh)\s+"
    r"(?:playwright|browser|chrome)(?:\s+session+)?\b|"
    r"\b(?:start|restart|reopen|launch)\s+(?:the\s+)?(?:playwright|browser|chrome)"
    r"(?:\s+session+)?\b",
    re.IGNORECASE,
)
_BROWSER_SESSION_TOOL_SUFFIXES = (
    "browser_navigate",
    "browser_snapshot",
    "browser_tabs",
    "browser_close",
)


def schema_tool_name(schema: Mapping[str, Any]) -> str:
    """Return a normalized function name from an OpenAI tool schema."""
    return str(schema.get("function", {}).get("name") or "").strip()


def categorize_tool_name(name: str) -> ToolCategory:
    """Classify a tool by user-facing purpose, conservatively for unknown MCPs."""
    normalized = str(name or "").strip()
    lowered = normalized.casefold()
    if normalized in DELEGATION_TOOL_NAMES:
        return ToolCategory.DELEGATION
    if normalized in CORE_TOOL_NAMES:
        return ToolCategory.CORE_CONVERSATION
    if normalized in SKILL_ROUTING_TOOL_NAMES or normalized in SKILL_MUTATION_TOOL_NAMES:
        return ToolCategory.SKILLS
    if normalized in SYSTEM_MUTATION_TOOL_NAMES:
        return ToolCategory.SYSTEM
    if normalized in WORKFLOW_TOOL_NAMES:
        return ToolCategory.WORKFLOWS
    if normalized in RECALL_TOOL_NAMES or lowered.startswith((
        "store_memory", "update_memory", "delete_memory", "remember_person", "update_person", "forget_person",
        "review_learning",
    )):
        return ToolCategory.RECALL
    if normalized in FILE_TOOL_NAMES:
        return ToolCategory.FILES
    if normalized in CODE_TOOL_NAMES:
        return ToolCategory.CODE_EXECUTION
    if normalized in RESEARCH_TOOL_NAMES:
        return ToolCategory.RESEARCH
    if normalized in COMMUNICATION_TOOL_NAMES or any(
        marker in lowered for marker in ("gmail_send", "telegram_send", "send_email")
    ):
        return ToolCategory.COMMUNICATION
    if lowered.startswith("telephony_"):
        return ToolCategory.TELEPHONY
    if lowered.startswith("phone_"):
        return ToolCategory.PHONE
    if "watcher" in lowered or lowered.startswith(("create_monitor", "list_monitor", "update_monitor")):
        return ToolCategory.WATCHERS
    if lowered.startswith("vision_"):
        return ToolCategory.VISION
    if "goal" in lowered or "follow_up" in lowered:
        return ToolCategory.GOALS
    if lowered.startswith("mcp__playwright__") or lowered.startswith("browser_"):
        return ToolCategory.BROWSER_DOM
    if lowered.startswith("mcp__windows__"):
        return ToolCategory.DESKTOP_UIA
    if lowered.startswith("mcp__"):
        return ToolCategory.MCP
    # A new local/plugin tool is consequential until it has an explicit
    # registry category. Visibility selection is not an authorization
    # boundary, but this prevents accidental fail-open advertising as well.
    return ToolCategory.UNKNOWN_CONSEQUENTIAL


def is_harmless_read_tool(name: str) -> bool:
    """Return whether advertising the schema on a read-only turn is safe."""
    normalized = str(name or "").strip()
    lowered = normalized.casefold()
    category = categorize_tool_name(normalized)
    if normalized in AGENT_INTROSPECTION_TOOL_NAMES:
        return True
    if normalized in SKILL_ROUTING_TOOL_NAMES:
        return True
    if normalized in READ_ONLY_FILE_TOOL_NAMES or normalized in RECALL_TOOL_NAMES:
        return True
    if normalized in {"list_tasks", "get_task_status", "web_search", "fetch_url", "extract_document"}:
        return True
    if normalized in CORE_TOOL_NAMES:
        return True
    if normalized in VISION_READ_TOOL_NAMES:
        return True
    read_verbs = ("get", "list", "read", "search", "find", "fetch", "inspect", "snapshot", "status", "show")
    if category in {ToolCategory.GOALS, ToolCategory.WATCHERS, ToolCategory.PHONE, ToolCategory.TELEPHONY}:
        return lowered.startswith(read_verbs) or any(
            f"_{verb}_" in f"_{lowered}_" for verb in read_verbs
        )
    if category is ToolCategory.MCP:
        operation = lowered.split("__")[-1]
        return operation.startswith(read_verbs)
    return False


@dataclass(frozen=True, slots=True)
class RegisteredTool:
    name: str
    category: ToolCategory
    schema: Mapping[str, Any]


class RootToolRegistry:
    """Keep the complete schema inventory and expose a small per-turn subset."""

    def __init__(self, schemas: Iterable[Mapping[str, Any]] = ()) -> None:
        self._tools: dict[str, RegisteredTool] = {}
        for schema in schemas:
            self.register(schema)

    def register(self, schema: Mapping[str, Any], *, replace: bool = False) -> None:
        name = schema_tool_name(schema)
        if not name:
            raise ValueError("tool schema is missing function.name")
        if name in self._tools and not replace:
            raise ValueError(f"tool {name!r} is already registered")
        # Retain a private copy so later mutations to an MCP manager's list do
        # not silently change a selection already being built.
        stored = copy.deepcopy(dict(schema))
        self._tools[name] = RegisteredTool(name, categorize_tool_name(name), stored)

    def names(self) -> tuple[str, ...]:
        return tuple(self._tools)

    def categories(self) -> dict[ToolCategory, tuple[str, ...]]:
        grouped: dict[ToolCategory, list[str]] = {category: [] for category in ToolCategory}
        for tool in self._tools.values():
            grouped[tool.category].append(tool.name)
        return {category: tuple(names) for category, names in grouped.items() if names}

    def schemas_for_category(self, category: ToolCategory) -> list[dict[str, Any]]:
        return [
            copy.deepcopy(dict(tool.schema))
            for tool in self._tools.values()
            if tool.category is category
        ]

    @staticmethod
    def _intent_value(context: Any) -> str:
        intent = getattr(context, "intent", context)
        return str(getattr(intent, "value", intent) or "").casefold()

    @staticmethod
    def _decision_mode(decision: Any | None) -> str:
        mode = getattr(decision, "mode", "") if decision is not None else ""
        return str(getattr(mode, "value", mode) or "").casefold()

    def select_for_turn(
        self,
        context: Any,
        *,
        delegation_decision: Any | None = None,
    ) -> list[dict[str, Any]]:
        """Return the narrow schema set appropriate for the current turn."""
        intent = self._intent_value(context)
        mode = self._decision_mode(delegation_decision)
        should_delegate = bool(getattr(delegation_decision, "should_delegate", False))
        text = str(getattr(context, "user_input", "") or "").casefold()
        targets = {str(value).casefold() for value in getattr(context, "explicit_targets", ())}
        grant_names = {
            str(getattr(grant, "tool_name", "") or "")
            for grant in getattr(context, "confirmation_grants", ())
            if getattr(grant, "tool_name", "")
        }

        explicit_delegation = intent == "delegation" or mode == "explicit" or should_delegate
        agent_meta = mode == "meta" or bool(targets & {"agent", "agents", "agent_runs"})
        if explicit_delegation:
            return self._schemas_named(
                set(DELEGATION_TOOL_NAMES) | set(CORE_TOOL_NAMES) | grant_names
            )
        if agent_meta:
            return self._schemas_named(set(AGENT_INTROSPECTION_TOOL_NAMES) | grant_names)

        # Keep the Hermes workflow usable from every chat surface while
        # avoiding two extra schemas on unrelated turns.
        learning_review = any(
            marker in text
            for marker in (
                "learning review", "learning proposal", "procedural learning",
                "hermes review", "approve learning", "reject learning",
            )
        ) or bool(re.search(r"\b(?:approve|reject)\s+#?\s*\d+\b", text))
        if learning_review:
            return self._schemas_named({
                "list_learning_reviews", "review_learning", "search_memory", *grant_names,
            })
        if intent in {"conversation", "confirmation_response"}:
            return self._schemas_named(grant_names)

        # The normal root model is itself the semantic router. Ambiguous
        # natural-language tasks receive the live catalog once, then native
        # tool calling chooses the capability from descriptions instead of a
        # growing list of English keyword rules.
        if intent == "model_routed":
            return [
                copy.deepcopy(dict(tool.schema))
                for tool in self._tools.values()
            ]

        if intent == "browser_interaction" and _BROWSER_SESSION_TURN_RE.search(text):
            browser_session_names = {
                tool.name
                for tool in self._tools.values()
                if tool.category is ToolCategory.BROWSER_DOM
                and tool.name.casefold().endswith(_BROWSER_SESSION_TOOL_SUFFIXES)
            }
            return self._schemas_named(
                browser_session_names
                | set(CORE_TOOL_NAMES)
                | set(SKILL_ROUTING_TOOL_NAMES)
                | grant_names
            )

        if "prior_task" in targets and intent == "local_mutation":
            return self._schemas_named({
                "search_memory", "search_actions", "list_tasks", "get_task_status",
                "run_task", *grant_names,
            })

        if intent == "read_only":
            if "prior_task" in targets:
                return self._schemas_named({
                    "search_memory", "search_actions", "list_tasks", "get_task_status",
                    "list_agent_runs", "get_latest_agent_run", "get_agent_run",
                    *grant_names,
                })
            selected_reads: list[dict[str, Any]] = []
            for tool in self._tools.values():
                if tool.name in grant_names:
                    selected_reads.append(copy.deepcopy(dict(tool.schema)))
                    continue
                # A terse live visual request can still arrive with a
                # read-only grammatical shape ("what can you see now?").
                # Keep current capture available whenever Vision is the
                # explicit surface instead of forcing the model to inspect
                # yesterday's event history.
                if "vision" in targets and tool.name in VISION_LIVE_TOOL_NAMES:
                    selected_reads.append(copy.deepcopy(dict(tool.schema)))
                    continue
                if not is_harmless_read_tool(tool.name):
                    continue
                if tool.category is ToolCategory.MCP:
                    parts = tool.name.casefold().split("__")
                    server = parts[1] if len(parts) > 2 else ""
                    if not any(marker in text for marker in ("mcp", "connector", "integration", server)):
                        continue
                selected_reads.append(copy.deepcopy(dict(tool.schema)))
            return selected_reads

        allowed_categories: set[ToolCategory] = {ToolCategory.CORE_CONVERSATION}
        allowed_names: set[str] = (
            set(CORE_TOOL_NAMES) | set(SKILL_ROUTING_TOOL_NAMES) | grant_names
        )
        if intent == "local_mutation":
            if "recall" in targets:
                allowed_categories.add(ToolCategory.RECALL)
            if targets & {"filesystem", "code", "research"}:
                allowed_categories.add(ToolCategory.FILES)
            if "code" in targets:
                allowed_categories.add(ToolCategory.CODE_EXECUTION)
            if targets & {"workflow", "prior_task"}:
                allowed_categories.add(ToolCategory.WORKFLOWS)
            if "goals" in targets:
                allowed_categories.add(ToolCategory.GOALS)
            if "watchers" in targets:
                allowed_categories.add(ToolCategory.WATCHERS)
            if "vision" in targets:
                allowed_categories.add(ToolCategory.VISION)
            if "research" in targets:
                allowed_categories.add(ToolCategory.RESEARCH)
            if "config" in targets:
                allowed_names.add("update_config")
        elif intent == "browser_interaction":
            allowed_categories |= {ToolCategory.BROWSER_DOM, ToolCategory.RESEARCH}
        elif intent == "desktop_interaction":
            allowed_categories.add(ToolCategory.DESKTOP_UIA)
        elif intent == "external_action":
            allowed_categories |= {
                ToolCategory.COMMUNICATION, ToolCategory.PHONE, ToolCategory.TELEPHONY,
                ToolCategory.MCP,
            }
            if any(marker in text for marker in ("browser", "website", "page", "site", "url")):
                allowed_categories.add(ToolCategory.BROWSER_DOM)

        selected: list[dict[str, Any]] = []
        for tool in self._tools.values():
            if tool.name in allowed_names or tool.category in allowed_categories:
                if tool.category is ToolCategory.CORE_CONVERSATION and tool.name not in allowed_names:
                    continue
                if (
                    intent == "browser_interaction"
                    and tool.category is ToolCategory.RESEARCH
                    and not is_harmless_read_tool(tool.name)
                ):
                    continue
                selected.append(copy.deepcopy(dict(tool.schema)))
        return selected

    def _schemas_named(self, names: set[str]) -> list[dict[str, Any]]:
        return [
            copy.deepcopy(dict(tool.schema))
            for tool in self._tools.values()
            if tool.name in names
        ]


def select_root_tools(
    schemas: Sequence[Mapping[str, Any]],
    context: Any,
    *,
    delegation_decision: Any | None = None,
) -> list[dict[str, Any]]:
    """Convenience wrapper for callers that rebuild MCP schemas each turn."""
    return RootToolRegistry(schemas).select_for_turn(
        context, delegation_decision=delegation_decision
    )


__all__ = [
    "AGENT_INTROSPECTION_TOOL_NAMES",
    "DELEGATION_TOOL_NAMES",
    "RootToolRegistry",
    "RegisteredTool",
    "ToolCategory",
    "VISION_READ_TOOL_NAMES",
    "WORKFLOW_TOOL_NAMES",
    "categorize_tool_name",
    "is_harmless_read_tool",
    "schema_tool_name",
    "select_root_tools",
]
