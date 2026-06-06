from __future__ import annotations

from .core import JsonObject, ToolContext, ToolResult, ToolSpec, schema
from .project_common import run_project_action


SPEC = ToolSpec(
    name="task_bulk_update",
    description=(
        "Update several existing tasks together by project, current status, or explicit task ids/titles. "
        "Use for bulk priority, due date/time, recurrence, tags, or status changes."
    ),
    parameters=schema(
        {
            "project": {"type": "string", "description": "Project name or id."},
            "task_ids": {"type": "array", "items": {"type": "string"}},
            "match_status": {
                "type": "string",
                "enum": ["pending", "in_progress", "blocked", "done", "archived"],
            },
            "status": {
                "type": "string",
                "enum": ["pending", "in_progress", "blocked", "done", "archived"],
            },
            "priority": {"type": "string", "enum": ["low", "normal", "high", "urgent"]},
            "due": {"type": "string"},
            "tags": {"type": "array", "items": {"type": "string"}},
            "recurrence": {"type": "string"},
            "include_done": {"type": "boolean", "default": False},
        }
    ),
)


def run(ctx: ToolContext, args: JsonObject) -> ToolResult:
    return run_project_action(SPEC.name, "task_bulk_update", ctx, args)
