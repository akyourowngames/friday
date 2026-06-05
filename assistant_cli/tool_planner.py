from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from .nvidia_chat import NvidiaChat
from .tools import ToolRegistry, ToolResult


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
        tools = [
            {
                "name": spec.name,
                "description": spec.description,
                "parameters": spec.parameters,
                "examples": list(spec.examples),
            }
            for spec in self.registry.specs()
        ]
        payload = {
            "latest_user_message": str(user_text or ""),
            "recent_conversation": conversation_messages[-8:],
            "registered_tools": tools,
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
                max_tokens=500,
                timeout=self.chat.settings.tool_planner_timeout_seconds,
            )
        except Exception:
            return NO_TOOL_PLAN
        return parse_tool_plan(raw, set(self.registry.names()))


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
