from __future__ import annotations

from .core import JsonObject, ToolContext, ToolResult, ToolSpec, schema
from .project_common import run_project_action


SPEC = ToolSpec(
    name="task_update",
    description="Update one existing task's title, description, status, priority, due date/time, tags, or recurrence.",
    parameters=schema(
        {
            "task_id": {"type": "string", "description": "Task id. Preferred when known."},
            "task": {"type": "string", "description": "Exact or partial task title."},
            "project": {"type": "string", "description": "Project name or id for disambiguation."},
            "title": {"type": "string"},
            "description": {"type": "string"},
            "status": {
                "type": "string",
                "enum": ["pending", "in_progress", "blocked", "done", "archived"],
            },
            "priority": {"type": "string", "enum": ["low", "normal", "high", "urgent"]},
            "due": {"type": "string", "description": "Natural or ISO due date/time."},
            "tags": {"type": "array", "items": {"type": "string"}},
            "recurrence": {"type": "string"},
        }
    ),
)


def run(ctx: ToolContext, args: JsonObject) -> ToolResult:
    return run_project_action(SPEC.name, "task_update", ctx, args)
