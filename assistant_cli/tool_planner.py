from __future__ import annotations

import json
from dataclasses import dataclass
from math import sqrt
from typing import Any

from .nvidia_chat import NvidiaChat
from .tools import ToolRegistry, ToolResult, ToolSpec


@dataclass(frozen=True)
class ToolPlan:
    tool: str
    arguments: dict[str, Any]
    confidence: float
    reason: str = ""

    @property
    def uses_tool(self) -> bool:
        return self.tool != "none"

    def meets_confidence(self, minimum: float) -> bool:
        return self.uses_tool and self.confidence >= minimum


NO_TOOL_PLAN = ToolPlan(tool="none", arguments={}, confidence=0.0)


class ToolPlanner:
    def __init__(self, chat: NvidiaChat, registry: ToolRegistry) -> None:
        self.chat = chat
        self.registry = registry

    def plan(
        self,
        user_text: str,
        conversation_messages: list[dict[str, str]],
        recent_tool_results: list[dict[str, Any]] | None = None,
    ) -> ToolPlan:
        candidate_specs = candidate_tool_specs(
            user_text,
            self.registry.specs(),
            threshold=self.chat.settings.tool_prefilter_threshold,
            max_candidates=self.chat.settings.tool_prefilter_max_candidates,
        )
        if not candidate_specs:
            return NO_TOOL_PLAN

        tools = [
            {
                "name": spec.name,
                "description": spec.description,
                "parameters": _compact_parameters(spec.parameters),
                "examples": list(spec.examples),
            }
            for spec in candidate_specs
        ]
        payload = {
            "latest_user_message": str(user_text or ""),
            "recent_conversation": conversation_messages[-4:],
            "recent_tool_results": compact_recent_tool_results(recent_tool_results or [], max_items=6),
            "candidate_tools": tools,
            "output_contract": {
                "tool": "registered tool name or none",
                "arguments": "object of arguments for the chosen tool",
                "confidence": "number from 0 to 1",
                "reason": "short private routing note",
            },
        }
        messages = [
            {
                "role": "system",
                "content": (
                    "You are Friday's tool router. Decide whether the latest user message needs one registered tool. "
                    "Do not answer the user. Do not invent tools. Return JSON only with tool, arguments, confidence, and reason. "
                    "The latest user message must itself ask for action, verification, fresh data, or a concrete operation; "
                    "do not route a pure acknowledgement or social reaction just because recent context mentioned a tool. "
                    "Choose a tool when current data, weather, location lookup, calculation, conversion, file access, "
                    "URL fetch, notes, project/task management, encoding, IDs, hashes, or random/password generation "
                    "would materially help. "
                    "For project/task mutation requests, choose project_manage; do not claim changes without a tool. "
                    "If the user asks to mark all tasks done, complete every open task, or mark them done after a task list, "
                    "choose project_manage with action task_complete_all and the project when known. "
                    "If the user says to mark it/that/this done after a task_create result, choose project_manage "
                    "with action task_complete and that task_id from recent_tool_results. "
                    "If the user asks to create or add a task and says it is called, named, or titled something, "
                    "choose task_create and put that exact task name in the title argument; never turn an add/create task "
                    "request into a completion action because of older context. "
                    "If the user asks for pending tasks, include status pending. "
                    "If the user asks to list projects or list my project, choose summary or project_list, not notes or files. "
                    "If the user asks to double-check task/project state after a project/task turn, choose project_manage "
                    "with summary; never use a mutation action for double-checking or verification. "
                    "Choose none for casual chat, opinions, memory-only questions, or when no registered tool fits. "
                    "The assistant will use the tool result later to write the natural answer."
                ),
            },
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ]
        try:
            raw = self.chat.complete(
                messages,
                temperature=0,
                max_tokens=220,
                timeout=self.chat.settings.tool_planner_timeout_seconds,
                model=self.chat.settings.tool_planner_model,
            )
        except Exception:
            return NO_TOOL_PLAN
        plan = parse_tool_plan(raw, set(self.registry.names()))
        plan = repair_tool_plan_scope(plan, user_text, recent_tool_results or [])
        if not plan.uses_tool:
            return project_continuation_plan(user_text, recent_tool_results or []) or plan
        return plan


def candidate_tool_specs(
    user_text: str,
    specs: list[ToolSpec],
    threshold: float,
    max_candidates: int,
) -> list[ToolSpec]:
    user_terms = _terms(user_text)
    if not user_terms:
        return []

    scored: list[tuple[float, ToolSpec]] = []
    for spec in specs:
        catalog = " ".join([spec.name, spec.description, " ".join(spec.examples)])
        score = _cosine(user_terms, _terms(catalog))
        if score >= threshold:
            scored.append((score, spec))
    scored.sort(key=lambda item: item[0], reverse=True)
    return [spec for _, spec in scored[: max(1, int(max_candidates))]]


def _compact_parameters(parameters: dict[str, Any]) -> dict[str, Any]:
    properties = parameters.get("properties", {})
    if not isinstance(properties, dict):
        properties = {}
    return {
        "required": parameters.get("required", []),
        "fields": {
            name: {
                "type": value.get("type") if isinstance(value, dict) else None,
                "description": value.get("description") if isinstance(value, dict) else "",
                "default": value.get("default") if isinstance(value, dict) else None,
            }
            for name, value in properties.items()
        },
    }


def compact_recent_tool_results(records: list[dict[str, Any]], max_items: int = 6) -> list[dict[str, Any]]:
    compacted: list[dict[str, Any]] = []
    for record in records[-max(0, int(max_items)) :]:
        try:
            payload = json.loads(str(record.get("content") or ""))
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        data = payload.get("data", {})
        if not isinstance(data, dict):
            data = {}
        compacted.append(
            {
                "tool": record.get("tool") or payload.get("tool"),
                "ok": bool(payload.get("ok")),
                "text": str(payload.get("text") or "")[:1200],
                "data": _compact_tool_data(data),
            }
        )
    return compacted


def _compact_tool_data(data: dict[str, Any]) -> dict[str, Any]:
    keep: dict[str, Any] = {"action": data.get("action")}
    for key in ("project", "task"):
        value = data.get(key)
        if isinstance(value, dict):
            keep[key] = _compact_project_or_task(value)
    for key in ("projects", "tasks", "updated_tasks"):
        value = data.get(key)
        if isinstance(value, list):
            keep[key] = [_compact_project_or_task(item) for item in value[:8] if isinstance(item, dict)]
    for key in ("updated_count", "matched_count", "status"):
        if key in data:
            keep[key] = data[key]
    return {key: value for key, value in keep.items() if value not in (None, "", [])}


def _compact_project_or_task(item: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "id",
        "name",
        "title",
        "project_id",
        "project_name",
        "status",
        "priority",
        "open_tasks",
        "done_tasks",
    )
    return {key: item[key] for key in keys if key in item}


def _terms(text: str) -> dict[str, int]:
    terms: dict[str, int] = {}
    token: list[str] = []
    for char in str(text or "").lower():
        if char.isalnum():
            token.append(char)
            continue
        _flush_term(token, terms)
    _flush_term(token, terms)
    return terms


def _flush_term(token: list[str], terms: dict[str, int]) -> None:
    if not token:
        return
    value = "".join(token)
    token.clear()
    if len(value) < 2 and not any(char.isdigit() for char in value):
        return
    terms[value] = terms.get(value, 0) + 1


def _cosine(left: dict[str, int], right: dict[str, int]) -> float:
    if not left or not right:
        return 0.0
    common = set(left).intersection(right)
    numerator = sum(left[key] * right[key] for key in common)
    if numerator <= 0:
        return 0.0
    left_norm = sqrt(sum(value * value for value in left.values()))
    right_norm = sqrt(sum(value * value for value in right.values()))
    if left_norm <= 0 or right_norm <= 0:
        return 0.0
    return numerator / (left_norm * right_norm)


def parse_tool_plan(raw: str, valid_tools: set[str]) -> ToolPlan:
    data = _parse_json_object(raw)
    tool = str(data.get("tool") or data.get("name") or "none").strip()
    if not tool or tool.lower() == "none" or tool not in valid_tools:
        return NO_TOOL_PLAN

    arguments = data.get("arguments", {})
    if not isinstance(arguments, dict):
        arguments = {}
    confidence = _float(data.get("confidence"), default=0.0)
    reason = str(data.get("reason") or "").strip()
    return ToolPlan(tool=tool, arguments=arguments, confidence=max(0.0, min(1.0, confidence)), reason=reason)


def repair_tool_plan_scope(
    plan: ToolPlan,
    user_text: str,
    recent_tool_results: list[dict[str, Any]] | None = None,
) -> ToolPlan:
    if plan.tool != "project_manage":
        return plan
    action = _normalized_action(plan.arguments.get("action"))
    recent_tool_results = recent_tool_results or []
    title = _extract_named_title(user_text)
    if title and _wants_task_create(user_text) and action != "task_create":
        arguments = dict(plan.arguments)
        arguments["action"] = "task_create"
        arguments["title"] = title
        for key in ("task", "task_id", "all"):
            arguments.pop(key, None)
        reason = (plan.reason + "; " if plan.reason else "") + "task create scope"
        return ToolPlan(tool=plan.tool, arguments=arguments, confidence=plan.confidence, reason=reason)

    if action == "task_create" and not _has_any(plan.arguments, ("title", "task")):
        if title:
            arguments = dict(plan.arguments)
            arguments["title"] = title
        reason = (plan.reason + "; " if plan.reason else "") + "task title repaired"
        return ToolPlan(tool=plan.tool, arguments=arguments, confidence=plan.confidence, reason=reason)

    if action != "task_list" and _wants_pending_tasks(user_text):
        arguments = dict(plan.arguments)
        arguments["action"] = "task_list"
        arguments["status"] = "pending"
        if not str(arguments.get("project") or "").strip():
            project = _latest_project_name(recent_tool_results)
            if project:
                arguments["project"] = project
        reason = (plan.reason + "; " if plan.reason else "") + "pending task list scope"
        return ToolPlan(tool=plan.tool, arguments=arguments, confidence=plan.confidence, reason=reason)

    if action == "task_list" and _wants_pending_tasks(user_text) and not str(plan.arguments.get("status") or ""):
        arguments = dict(plan.arguments)
        arguments["status"] = "pending"
        if not str(arguments.get("project") or "").strip():
            project = _latest_project_name(recent_tool_results)
            if project:
                arguments["project"] = project
        reason = (plan.reason + "; " if plan.reason else "") + "pending task list status"
        return ToolPlan(tool=plan.tool, arguments=arguments, confidence=plan.confidence, reason=reason)

    if action == "task_list" and _has_verification_reference(user_text) and not str(plan.arguments.get("status") or ""):
        arguments = dict(plan.arguments)
        arguments["action"] = "summary"
        reason = (plan.reason + "; " if plan.reason else "") + "project verification summary"
        return ToolPlan(tool=plan.tool, arguments=arguments, confidence=plan.confidence, reason=reason)

    bulk_action = ""
    if action in {"task_complete", "complete_task", "complete"} and _has_bulk_reference(user_text):
        bulk_action = "task_complete_all"
    elif action in {"task_pending", "pending_task", "reopen_task", "reopen"} and _has_bulk_reference(user_text):
        bulk_action = "task_pending_all"
    if not bulk_action:
        return plan

    arguments = dict(plan.arguments)
    arguments["action"] = bulk_action
    arguments["all"] = True
    for key in ("task", "task_id", "title"):
        arguments.pop(key, None)
    reason = (plan.reason + "; " if plan.reason else "") + "bulk project task scope"
    return ToolPlan(tool=plan.tool, arguments=arguments, confidence=plan.confidence, reason=reason)


def project_continuation_plan(user_text: str, recent_tool_results: list[dict[str, Any]]) -> ToolPlan | None:
    if _wants_task_done(user_text):
        project = _latest_project_name(recent_tool_results)
        if _has_bulk_reference(user_text):
            arguments: dict[str, Any] = {"action": "task_complete_all", "all": True}
            if project:
                arguments["project"] = project
            return ToolPlan("project_manage", arguments, confidence=0.9, reason="recent bulk task completion")
        task = _latest_pending_task(recent_tool_results)
        if task:
            arguments = {"action": "task_complete", "task_id": task["id"]}
            if task.get("project_name"):
                arguments["project"] = task["project_name"]
            return ToolPlan("project_manage", arguments, confidence=0.9, reason="recent task completion")

    if _has_verification_reference(user_text):
        project = _latest_project_name(recent_tool_results)
        arguments = {"action": "summary"}
        if project:
            arguments["project"] = project
        return ToolPlan("project_manage", arguments, confidence=0.85, reason="recent project verification")

    return None


def _latest_pending_task(records: list[dict[str, Any]]) -> dict[str, Any] | None:
    for payload in reversed(compact_recent_tool_results(records, max_items=8)):
        data = payload.get("data", {})
        if not payload.get("ok") or not isinstance(data, dict):
            continue
        task = data.get("task")
        if isinstance(task, dict) and task.get("id") and task.get("status") != "done":
            return task
        for key in ("tasks", "updated_tasks"):
            tasks = data.get(key)
            if not isinstance(tasks, list):
                continue
            for item in tasks:
                if isinstance(item, dict) and item.get("id") and item.get("status") != "done":
                    return item
    return None


def _latest_project_name(records: list[dict[str, Any]]) -> str:
    for payload in reversed(compact_recent_tool_results(records, max_items=8)):
        data = payload.get("data", {})
        if not payload.get("ok") or not isinstance(data, dict):
            continue
        project = data.get("project")
        if isinstance(project, dict) and project.get("name"):
            return str(project["name"])
        projects = data.get("projects")
        if isinstance(projects, list):
            for item in projects:
                if isinstance(item, dict) and item.get("name"):
                    return str(item["name"])
        task = data.get("task")
        if isinstance(task, dict) and task.get("project_name"):
            return str(task["project_name"])
        for key in ("tasks", "updated_tasks"):
            tasks = data.get(key)
            if not isinstance(tasks, list):
                continue
            for item in tasks:
                if isinstance(item, dict) and item.get("project_name"):
                    return str(item["project_name"])
    return ""


def _normalized_action(value: object) -> str:
    return str(value or "").strip().lower().replace("-", "_").replace(" ", "_")


def _has_any(mapping: dict[str, Any], keys: tuple[str, ...]) -> bool:
    return any(str(mapping.get(key) or "").strip() for key in keys)


def _extract_named_title(text: str) -> str:
    raw = str(text or "").strip()
    lowered = raw.lower()
    for marker in (" called ", " named ", " titled "):
        index = lowered.find(marker)
        if index == -1:
            continue
        title = raw[index + len(marker) :].strip(" \t\r\n'\"`.,;:")
        return title[:160].strip()
    return ""


def _wants_task_create(text: str) -> bool:
    terms = set(_terms(text))
    return bool(terms.intersection({"add", "create", "new"})) and "task" in terms


def _wants_task_done(text: str) -> bool:
    terms = set(_terms(text))
    return bool(terms.intersection({"mark", "complete", "finish"})) and bool(terms.intersection({"done", "complete"}))


def _wants_pending_tasks(text: str) -> bool:
    terms = set(_terms(text))
    return "pending" in terms and bool(terms.intersection({"task", "tasks"}))


def _has_verification_reference(text: str) -> bool:
    terms = set(_terms(text))
    return "check" in terms or bool(terms.intersection({"verify", "confirmed", "confirm"}))


def _has_bulk_reference(text: str) -> bool:
    terms = set(_terms(text))
    return bool(terms.intersection({"all", "them", "those", "both", "everything"}))


def tool_result_context(plan: ToolPlan, result: ToolResult, max_chars: int) -> str:
    payload = {
        "planned_tool": plan.tool,
        "arguments": plan.arguments,
        "confidence": plan.confidence,
        "tool_result": result.as_dict(),
    }
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    limit = max(1000, int(max_chars))
    if len(text) > limit:
        text = text[:limit].rsplit("\n", 1)[0] + "\n... [truncated]"
    return (
        "CURRENT TURN TOOL RESULT - SOURCE OF TRUTH. "
        "A registered local tool was executed for the current user request. "
        "Use this tool result to answer naturally in your own words, but keep every project/task count, status, title, "
        "task id, project id, and changed item exactly consistent with tool_result. "
        "The current tool_result overrides earlier assistant messages and saved context if they disagree. "
        "Never invent extra tasks, ids, changed rows, totals, or existing items. "
        "For current data requests, do not add saved memory or read-only project snapshot facts that are not present "
        "in tool_result. "
        "Prefer task/project titles and counts in the natural reply; only include internal ids when the user asks for ids "
        "or an id is needed to disambiguate, and then copy ids exactly from tool_result. "
        "Do not tell the user to install or run another weather/search/tool command when this result answers the request. "
        "For project/task create, update, complete, pending, archive, or delete requests, only say the change happened "
        "when tool_result.ok is true and the result data proves the changed state. "
        "If a project/task mutation tool failed, say no change was made; do not claim you will run another hidden step. "
        "If a bulk task mutation reports updated_count 0, say no tasks were changed. "
        "For project summaries, report open_tasks and done_tasks exactly from tool_result.data.projects. "
        "For task lists or bulk updates, mention only tasks present in tool_result.data.tasks or updated_tasks. "
        "If the tool failed, explain the failure briefly and what exact setting or input is missing.\n\n"
        f"{text}"
    )


def _parse_json_object(raw: str) -> dict[str, Any]:
    text = str(raw or "").strip()
    try:
        value = json.loads(text)
        return value if isinstance(value, dict) else {}
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            return {}
        try:
            value = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return {}
        return value if isinstance(value, dict) else {}


def _float(value: object, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
