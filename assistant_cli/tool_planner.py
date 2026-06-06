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

    def plan(self, user_text: str, conversation_messages: list[dict[str, str]]) -> ToolPlan:
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
                    "Do not answer the user. Do not invent tools. Return JSON only. "
                    "Choose a tool when current data, weather, location lookup, calculation, conversion, file access, "
                    "URL fetch, notes, encoding, IDs, hashes, or random/password generation would materially help. "
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
        return parse_tool_plan(raw, set(self.registry.names()))


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
        "A registered local tool was executed for the current user request. "
        "Use this tool result to answer naturally in your own words. "
        "Do not tell the user to install or run another weather/search/tool command when this result answers the request. "
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
