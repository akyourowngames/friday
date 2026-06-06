from __future__ import annotations

from .core import JsonObject, ToolContext, ToolResult, ToolSpec, schema
from .project_common import run_project_action


SPEC = ToolSpec(
    name="task_list",
    description="List persisted tasks, optionally filtered by project and status.",
    parameters=schema(
        {
            "project": {"type": "string", "description": "Project name or id."},
            "status": {
                "type": "string",
                "enum": ["pending", "in_progress", "blocked", "done", "archived"],
            },
            "include_done": {"type": "boolean", "default": False},
            "include_archived": {"type": "boolean", "default": False},
            "limit": {"type": "integer", "default": 30},
        }
    ),
)


def run(ctx: ToolContext, args: JsonObject) -> ToolResult:
    return run_project_action(SPEC.name, "task_list", ctx, args)
