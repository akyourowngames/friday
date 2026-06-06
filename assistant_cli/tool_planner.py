from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .nvidia_chat import NvidiaChat
from .tools import ToolRegistry, ToolResult


FALLBACK_ROUTER_PROMPT = """You are Friday's tool router.
Use native function calls for real reads and mutations. Do not answer the user.
Resolve references from recent conversation and tool results.
Use no tool only when the latest turn does not need registered capabilities."""

PLAN_REVIEW_TOOL = {
    "type": "function",
    "function": {
        "name": "submit_intent_review",
        "description": "Return an independent semantic verdict for the latest user turn and proposed tool calls.",
        "parameters": {
            "type": "object",
            "properties": {
                "intent": {
                    "type": "string",
                    "enum": [
                        "social",
                        "brainstorming",
                        "read",
                        "mutation",
                        "project_definition",
                        "continuation",
                        "ambiguous",
                    ],
                    "description": "Semantic intent of the latest user message.",
                },
                "requested_operation": {
                    "type": "string",
                    "enum": [
                        "none",
                        "project_create",
                        "project_read",
                        "project_update",
                        "project_archive",
                        "task_create",
                        "task_read",
                        "task_update",
                        "task_complete",
                        "task_reopen",
                        "task_delete",
                        "other",
                    ],
                    "description": "The concrete operation requested by the latest user turn.",
                },
                "current_action_requested": {
                    "type": "boolean",
                    "description": "Whether the latest turn asks to read or change state, including resolved references.",
                },
                "complete_project_definition": {
                    "type": "boolean",
                    "description": (
                        "True only when the latest turn communicates a complete meaning or purpose for the current "
                        "project. False for a bare topic or category."
                    ),
                },
                "bare_fragment": {
                    "type": "boolean",
                    "description": (
                        "True when the latest message is only a topic, category, noun phrase, or fragment rather than "
                        "an action request or complete project definition."
                    ),
                },
                "calls_faithful": {
                    "type": "boolean",
                    "description": "Whether the proposed tools and arguments directly implement the authorized intent.",
                },
                "references_present": {
                    "type": "boolean",
                    "description": "Whether the latest turn relies on pronouns or contextual object references.",
                },
                "references_resolved": {
                    "type": "boolean",
                    "description": "Whether every reference needed by the proposed calls is grounded.",
                },
                "set_reference_present": {
                    "type": "boolean",
                    "description": "Whether the latest turn refers to a previously enumerated set of items.",
                },
                "coverage_complete": {
                    "type": "boolean",
                    "description": "Whether every requested or referenced item is represented in the proposed calls.",
                },
                "reason": {"type": "string"},
            },
            "required": [
                "intent",
                "requested_operation",
                "current_action_requested",
                "complete_project_definition",
                "bare_fragment",
                "calls_faithful",
                "references_present",
                "references_resolved",
                "set_reference_present",
                "coverage_complete",
                "reason",
            ],
            "additionalProperties": False,
        },
    },
}


@dataclass(frozen=True)
class PlannedToolCall:
    tool: str
    arguments: dict[str, Any]
    call_id: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"tool": self.tool, "arguments": self.arguments, "call_id": self.call_id}


@dataclass(frozen=True)
class ToolPlan:
    calls: tuple[PlannedToolCall, ...] = ()
    error: str = ""
    rejection: str = ""
    review: dict[str, Any] | None = None

    @property
    def uses_tool(self) -> bool:
        return bool(self.calls)

    def as_dict(self) -> dict[str, Any]:
        return {
            "calls": [call.as_dict() for call in self.calls],
            "error": self.error,
            "rejection": self.rejection,
            "review": self.review or {},
        }


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
        compact_results = compact_recent_tool_results(recent_tool_results or [], max_items=8)
        router_prompt = self._router_prompt()
        if compact_results:
            router_prompt += (
                "\n\nRecent executed tool results are authoritative context for references in the latest turn:\n"
                + json.dumps(compact_results, ensure_ascii=False)
            )
        messages: list[dict[str, str]] = [{"role": "system", "content": router_prompt}]
        messages.extend(conversation_messages[-12:])
        if not messages or messages[-1].get("role") != "user":
            messages.append({"role": "user", "content": str(user_text or "")})

        raw_calls, errors = self._choose(messages, self.registry.openai_schemas(auto_only=True))
        if raw_calls is None:
            return ToolPlan(error=" | ".join(errors))

        plan = ToolPlan(calls=self._validated_calls(raw_calls))
        if not plan.uses_tool:
            return plan
        review = self._review_plan(user_text, conversation_messages, compact_results, plan)
        if review["error"]:
            return ToolPlan(error=review["error"], review=review)
        if review["execute"]:
            return ToolPlan(calls=plan.calls, review=review)
        if review["intent"] in {"social", "brainstorming", "ambiguous"} or review["bare_fragment"]:
            return ToolPlan(rejection=review["reason"], review=review)

        corrected = self._correct_plan(messages, plan, review)
        if not corrected.uses_tool:
            return ToolPlan(rejection=review["reason"], review=review)
        if (
            (
                review["requested_operation"] == "project_create"
                and review["current_action_requested"]
                and all(call.tool == "project_create" for call in corrected.calls)
            )
            or (
                review["intent"] == "project_definition"
                and review["requested_operation"] == "project_update"
                and review["complete_project_definition"]
                and all(call.tool == "project_update" for call in corrected.calls)
            )
        ):
            corrected_review = dict(review)
            corrected_review["execute"] = True
            corrected_review["reason"] = "Corrected project definition to the dedicated project metadata tool."
            return ToolPlan(calls=corrected.calls, review=corrected_review)
        corrected_review = self._review_plan(user_text, conversation_messages, compact_results, corrected)
        if corrected_review["error"]:
            return ToolPlan(error=corrected_review["error"], review=corrected_review)
        if corrected_review["execute"]:
            return ToolPlan(calls=corrected.calls, review=corrected_review)
        return ToolPlan(rejection=corrected_review["reason"], review=corrected_review)

    def _correct_plan(
        self,
        original_messages: list[dict[str, str]],
        rejected_plan: ToolPlan,
        review: dict[str, Any],
    ) -> ToolPlan:
        messages = list(original_messages)
        correction = {
            "rejected_calls": [call.as_dict() for call in rejected_plan.calls],
            "independent_review": review,
            "instruction": (
                "Re-plan the same latest user turn. Make no tool call when the review identifies social, "
                "brainstorming, ambiguous, unresolved, or incomplete intent. Otherwise correct the tool and arguments."
            ),
        }
        insert_at = max(1, len(messages) - 1)
        messages.insert(
            insert_at,
            {
                "role": "system",
                "content": "The independent verifier rejected the first plan:\n" + json.dumps(correction, ensure_ascii=False),
            },
        )
        schemas = self.registry.openai_schemas(auto_only=True)
        tool_choice: Any = "auto"
        forced_tool = _tool_for_requested_operation(str(review.get("requested_operation") or ""))
        if forced_tool:
            schemas = [
                item
                for item in schemas
                if item.get("function", {}).get("name") == forced_tool
            ]
            tool_choice = {"type": "function", "function": {"name": forced_tool}}
        raw_calls, errors = self._choose(messages, schemas, tool_choice=tool_choice)
        if raw_calls is None:
            return ToolPlan(error=" | ".join(errors), review=review)
        return ToolPlan(calls=self._validated_calls(raw_calls), review=review)

    def _choose(
        self,
        messages: list[dict[str, str]],
        schemas: list[dict[str, Any]],
        tool_choice: Any = "auto",
        models: list[str] | None = None,
    ) -> tuple[list[dict[str, Any]] | None, list[str]]:
        errors: list[str] = []
        if models is None:
            models = [self.chat.settings.tool_planner_model]
            fallback = self.chat.settings.tool_planner_fallback_model
            if fallback and fallback not in models:
                models.append(fallback)
        attempts = max(1, int(self.chat.settings.tool_planner_retries) + 1)
        for model in models:
            for _ in range(attempts):
                try:
                    calls = self.chat.choose_tools(
                        messages=messages,
                        tools=schemas,
                        timeout=self.chat.settings.tool_planner_timeout_seconds,
                        model=model,
                        tool_choice=tool_choice,
                    )
                    return calls, errors
                except Exception as exc:
                    errors.append(f"{model}: {type(exc).__name__}: {exc}")
        return None, errors

    def _validated_calls(self, raw_calls: list[dict[str, Any]]) -> tuple[PlannedToolCall, ...]:
        valid_tools = {spec.name for spec in self.registry.specs(auto_only=True)}
        calls: list[PlannedToolCall] = []
        max_calls = max(1, int(self.chat.settings.tool_planner_max_calls))
        for raw in raw_calls[:max_calls]:
            tool = str(raw.get("tool") or "").strip()
            arguments = raw.get("arguments", {})
            if tool not in valid_tools or not isinstance(arguments, dict):
                continue
            calls.append(
                PlannedToolCall(
                    tool=tool,
                    arguments=arguments,
                    call_id=str(raw.get("id") or ""),
                )
            )
        return tuple(calls)

    def _review_plan(
        self,
        user_text: str,
        conversation_messages: list[dict[str, str]],
        compact_results: list[dict[str, Any]],
        plan: ToolPlan,
    ) -> dict[str, Any]:
        payload = {
            "latest_user_message": str(user_text or ""),
            "immediate_previous_assistant": _immediate_previous_assistant(conversation_messages),
            "recent_tool_results": compact_results[-3:],
            "proposed_calls": [call.as_dict() for call in plan.calls],
        }
        messages = [
            {"role": "system", "content": self._verifier_prompt()},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ]
        raw, errors = self._choose(
            messages,
            [PLAN_REVIEW_TOOL],
            tool_choice={"type": "function", "function": {"name": "submit_intent_review"}},
            models=[
                self.chat.settings.tool_verifier_model,
                self.chat.settings.tool_verifier_fallback_model,
            ],
        )
        if raw is None or not raw:
            return {"execute": False, "reason": "", "error": " | ".join(errors) or "plan verifier returned no result"}
        arguments = raw[0].get("arguments", {})
        if not isinstance(arguments, dict):
            return {"execute": False, "reason": "", "error": "plan verifier returned invalid arguments"}
        intent = str(arguments.get("intent") or "ambiguous").strip()
        requested_operation = str(arguments.get("requested_operation") or "other").strip()
        current_action = bool(arguments.get("current_action_requested"))
        complete_definition = bool(arguments.get("complete_project_definition"))
        bare_fragment = bool(arguments.get("bare_fragment"))
        calls_faithful = bool(arguments.get("calls_faithful"))
        references_present = bool(arguments.get("references_present"))
        references_resolved = bool(arguments.get("references_resolved"))
        set_reference_present = bool(arguments.get("set_reference_present"))
        coverage_complete = bool(arguments.get("coverage_complete"))
        calls_are_project_updates = bool(plan.calls) and all(call.tool == "project_update" for call in plan.calls)
        allowed_tools = _tools_for_requested_operation(requested_operation)
        operation_match = not allowed_tools or all(call.tool in allowed_tools for call in plan.calls)
        semantic_match = operation_match if allowed_tools else calls_faithful
        reference_ok = not references_present or references_resolved
        coverage_ok = not set_reference_present or coverage_complete
        bulk_scope_tools = {"task_bulk_update", "task_complete_all", "task_reopen_all"}
        if plan.calls and all(call.tool in bulk_scope_tools for call in plan.calls):
            coverage_ok = True
        if current_action and intent not in {"social", "brainstorming", "ambiguous"}:
            execute = not bare_fragment and semantic_match and reference_ok and coverage_ok
        elif intent == "project_definition":
            execute = (
                complete_definition
                and not bare_fragment
                and calls_faithful
                and reference_ok
                and coverage_ok
                and calls_are_project_updates
            )
        elif intent == "continuation":
            execute = not bare_fragment and calls_faithful and reference_ok and coverage_ok
        else:
            execute = False
        reason = str(arguments.get("reason") or "").strip()
        if not execute and intent == "project_definition" and not current_action and not calls_are_project_updates:
            reason = "Project-purpose context may update project metadata, but it may not create a task."
        elif not execute and not coverage_complete:
            reason = reason or "The proposed calls do not preserve every requested item."
        return {
            "execute": execute,
            "intent": intent,
            "requested_operation": requested_operation,
            "current_action_requested": current_action,
            "complete_project_definition": complete_definition,
            "bare_fragment": bare_fragment,
            "calls_faithful": calls_faithful,
            "references_present": references_present,
            "references_resolved": references_resolved,
            "set_reference_present": set_reference_present,
            "coverage_complete": coverage_complete,
            "reason": reason or "The proposed calls do not faithfully match the latest turn.",
            "error": "",
        }

    def _router_prompt(self) -> str:
        return self._load_prompt(self.chat.settings.tool_router_prompt, FALLBACK_ROUTER_PROMPT)

    def _verifier_prompt(self) -> str:
        return self._load_prompt(
            self.chat.settings.tool_verifier_prompt,
            "Approve only tool calls explicitly supported by the latest user turn.",
        )

    @staticmethod
    def _load_prompt(raw_path: str, fallback: str) -> str:
        path = Path(raw_path)
        if not path.is_absolute():
            path = Path.cwd() / path
        try:
            prompt = path.read_text(encoding="utf-8").strip()
        except OSError:
            return fallback
        return prompt or fallback


def compact_recent_tool_results(records: list[dict[str, Any]], max_items: int = 8) -> list[dict[str, Any]]:
    compacted: list[dict[str, Any]] = []
    for record in records[-max(0, int(max_items)) :]:
        try:
            payload = json.loads(str(record.get("content") or ""))
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        data = payload.get("data", {})
        compacted.append(
            {
                "tool": record.get("tool") or payload.get("tool"),
                "ok": bool(payload.get("ok")),
                "text": str(payload.get("text") or "")[:1000],
                "data": _compact_tool_data(data if isinstance(data, dict) else {}),
            }
        )
    return compacted


def _immediate_previous_assistant(conversation_messages: list[dict[str, str]]) -> str:
    skipped_latest_user = False
    for message in reversed(conversation_messages):
        role = str(message.get("role") or "")
        if role == "user" and not skipped_latest_user:
            skipped_latest_user = True
            continue
        if role == "assistant":
            return str(message.get("content") or "")
    return ""


def _tools_for_requested_operation(operation: str) -> set[str]:
    return {
        "project_create": {"project_create"},
        "project_read": {"project_get", "project_list"},
        "project_update": {"project_update"},
        "project_archive": {"project_archive"},
        "task_create": {"task_create", "task_create_many"},
        "task_read": {"task_list"},
        "task_update": {
            "task_update",
            "task_bulk_update",
            "task_complete",
            "task_complete_all",
            "task_reopen_all",
        },
        "task_complete": {"task_complete", "task_complete_all", "task_update", "task_bulk_update"},
        "task_reopen": {"task_reopen_all", "task_update", "task_bulk_update"},
        "task_delete": {"task_delete"},
    }.get(operation, set())


def _tool_for_requested_operation(operation: str) -> str:
    forced = {
        "project_create": "project_create",
        "project_update": "project_update",
        "project_archive": "project_archive",
    }
    return forced.get(operation, "")


def _compact_tool_data(data: dict[str, Any]) -> dict[str, Any]:
    keep: dict[str, Any] = {}
    for key in (
        "action",
        "project",
        "projects",
        "task",
        "tasks",
        "created_tasks",
        "updated_tasks",
        "deleted_tasks",
        "created_count",
        "updated_count",
        "deleted_count",
        "status",
    ):
        value = data.get(key)
        if value not in (None, "", []):
            keep[key] = value
    return keep


def no_tool_executed_context(plan: ToolPlan) -> str:
    if plan.error:
        return (
            "No registered tool was executed because tool planning failed for this turn. "
            "Do not claim that any project, task, file, external state, or other tool-backed value was read or changed. "
            "Briefly say the action was not performed and ask the user to retry if they requested an operation. "
            "Do not say you are retrying, will retry, or performed a second hidden attempt. "
            f"Planner error: {plan.error}"
        )
    if plan.rejection:
        return (
            "No registered tool was executed because the proposed tool use did not faithfully match the current user turn. "
            "Do not claim any tool-backed read or mutation succeeded. Continue conversationally or ask one concise question "
            "if needed. Do not promise a hidden retry. "
            f"Review reason: {plan.rejection}"
        )
    return (
        "No registered tool was executed for this turn. Answer normal conversation naturally, but do not claim that "
        "any project, task, file, external state, or other tool-backed value was read, created, updated, completed, "
        "deleted, or verified. If the user requested such an operation, say it was not performed."
    )


def tool_results_context(plan: ToolPlan, results: list[ToolResult], max_chars: int) -> str:
    outcomes = []
    for result in results:
        data = result.data if isinstance(result.data, dict) else {}
        outcomes.append(
            {
                "tool": result.tool,
                "success": result.ok,
                "outcome": result.text,
                "newly_created": bool(data.get("created")) or int(data.get("created_count") or 0) > 0,
                "newly_updated": int(data.get("updated_count") or 0) > 0,
                "resulting_state": _compact_tool_data(data),
            }
        )
    payload = {"outcomes": outcomes}
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    limit = max(1000, int(max_chars))
    if len(text) > limit:
        text = text[:limit].rsplit("\n", 1)[0] + "\n... [truncated]"
    return (
        "CURRENT TURN TOOL RESULTS - AUTHORITATIVE SOURCE OF TRUTH. "
        "Write one concise natural response to the user's latest message using only this evidence. "
        "Each outcome happened just now in the current turn. "
        "Never invent projects, tasks, identifiers, counts, priorities, dates, statuses, or successful actions. "
        "success=true means the operation succeeded; never call it failed. "
        "success=false means it failed; never imply it succeeded. "
        "Do not announce that you will perform another mutation unless another successful result in this same payload proves it. "
        "When results include counts or item lists, keep them exactly consistent. "
        "newly_created=true means the item was created now; never say it already existed. "
        "newly_updated=true means state changed now; never say it was already in that state. "
        "Do not add brainstormed suggestions or unrelated follow-up work to a completed tool response. "
        "Prefer user-facing names over internal ids unless ids were requested.\n\n"
        f"{text}"
    )
