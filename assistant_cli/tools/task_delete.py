from __future__ import annotations

from .core import JsonObject, ToolContext, ToolResult, ToolSpec, fail, schema
from .project_common import run_project_action


SPEC = ToolSpec(
    name="task_delete",
    description="Permanently delete one task. Use only when the user explicitly requests deletion.",
    parameters=schema(
        {
            "task_id": {"type": "string"},
            "task": {"type": "string"},
            "project": {"type": "string"},
        }
    ),
)


def run(ctx: ToolContext, args: JsonObject) -> ToolResult:
    if not str(args.get("task_id") or args.get("task") or "").strip():
        return fail(SPEC.name, "task_id or task is required")
    return run_project_action(SPEC.name, "task_delete", ctx, args)
