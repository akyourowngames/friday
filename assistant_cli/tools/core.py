from __future__ import annotations

import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable

import httpx

from assistant_cli.config import Settings


JsonObject = dict[str, Any]
ToolHandler = Callable[["ToolContext", JsonObject], "ToolResult"]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: JsonObject
    examples: tuple[str, ...] = ()
    auto_route: bool = True

    def openai_schema(self) -> JsonObject:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


@dataclass(frozen=True)
class ToolResult:
    tool: str
    ok: bool
    text: str
    data: JsonObject
    latency_ms: int = 0

    def as_dict(self) -> JsonObject:
        return {
            "tool": self.tool,
            "ok": self.ok,
            "text": self.text,
            "data": self.data,
            "latency_ms": self.latency_ms,
        }


@dataclass(frozen=True)
class ToolContext:
    settings: Settings
    workspace_root: Path
    http: Any


class StatelessHttpClient:
    def __init__(self, timeout: float) -> None:
        self.timeout = timeout

    def get(self, url: str, **kwargs: Any) -> httpx.Response:
        return httpx.get(url, timeout=self.timeout, follow_redirects=True, **kwargs)

    def post(self, url: str, **kwargs: Any) -> httpx.Response:
        return httpx.post(url, timeout=self.timeout, follow_redirects=True, **kwargs)

    def close(self) -> None:
        return None


class ToolRegistry:
    def __init__(self, context: ToolContext) -> None:
        self.context = context
        self._specs: dict[str, ToolSpec] = {}
        self._handlers: dict[str, ToolHandler] = {}

    def register(self, spec: ToolSpec, handler: ToolHandler) -> None:
        if spec.name in self._specs:
            raise ValueError(f"Tool already registered: {spec.name}")
        self._specs[spec.name] = spec
        self._handlers[spec.name] = handler

    def names(self) -> list[str]:
        return sorted(self._specs)

    def specs(self, auto_only: bool = False) -> list[ToolSpec]:
        specs = [self._specs[name] for name in self.names()]
        if auto_only:
            specs = [spec for spec in specs if spec.auto_route]
        return specs

    def openai_schemas(self, auto_only: bool = False) -> list[JsonObject]:
        return [spec.openai_schema() for spec in self.specs(auto_only=auto_only)]

    def close(self) -> None:
        close = getattr(self.context.http, "close", None)
        if callable(close):
            close()

    def execute(self, name: str, args: JsonObject | None = None) -> ToolResult:
        tool_name = str(name or "").strip()
        if tool_name not in self._handlers:
            available = ", ".join(self.names())
            return ToolResult(
                tool=tool_name or "unknown",
                ok=False,
                text=f"Unknown tool. Available tools: {available}",
                data={"available": self.names()},
            )

        clean_args = args or {}
        if not isinstance(clean_args, dict):
            return ToolResult(
                tool=tool_name,
                ok=False,
                text="Tool arguments must be a JSON object.",
                data={"received_type": type(clean_args).__name__},
            )

        start = time.perf_counter()
        try:
            result = self._handlers[tool_name](self.context, clean_args)
        except Exception as exc:
            result = ToolResult(tool=tool_name, ok=False, text=str(exc), data={"error": type(exc).__name__})
        latency_ms = int((time.perf_counter() - start) * 1000)
        return replace(result, latency_ms=latency_ms)


def schema(properties: JsonObject, required: tuple[str, ...] = ()) -> JsonObject:
    return {
        "type": "object",
        "properties": properties,
        "required": list(required),
        "additionalProperties": False,
    }


def ok(tool: str, text: str, data: JsonObject | None = None) -> ToolResult:
    return ToolResult(tool=tool, ok=True, text=text, data=data or {})


def fail(tool: str, text: str, data: JsonObject | None = None) -> ToolResult:
    return ToolResult(tool=tool, ok=False, text=text, data=data or {})
