from __future__ import annotations

from .core import JsonObject, ToolContext, ToolResult, ToolSpec, fail, schema
from .project_common import run_project_action


SPEC = ToolSpec(
    name="task_complete",
    description="Mark one existing task done.",
    parameters=schema(
        {
            "task_id": {"type": "string", "description": "Task id. Preferred when known."},
            "task": {"type": "string", "description": "Task title."},
            "project": {"type": "string", "description": "Project for disambiguation."},
        }
    ),
)


def run(ctx: ToolContext, args: JsonObject) -> ToolResult:
    if not str(args.get("task_id") or args.get("task") or "").strip():
        return fail(SPEC.name, "task_id or task is required")
    return run_project_action(SPEC.name, "task_complete", ctx, args)
