from __future__ import annotations

from .core import JsonObject, ToolContext, ToolResult
from . import project_manage


def run_project_action(
    tool_name: str,
    action: str,
    ctx: ToolContext,
    args: JsonObject,
) -> ToolResult:
    result = project_manage.run(ctx, {**args, "action": action})
    return ToolResult(
        tool=tool_name,
        ok=result.ok,
        text=result.text,
        data=result.data,
        latency_ms=result.latency_ms,
    )
