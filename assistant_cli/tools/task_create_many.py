from __future__ import annotations

from .core import JsonObject, ToolContext, ToolResult, ToolSpec, schema
from .project_common import run_project_action


TASK_ITEM = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "description": {"type": "string"},
        "status": {"type": "string", "enum": ["pending", "in_progress", "blocked", "done"]},
        "priority": {"type": "string", "enum": ["low", "normal", "high", "urgent"]},
        "due": {"type": "string"},
        "tags": {"type": "array", "items": {"type": "string"}},
        "recurrence": {"type": "string"},
    },
    "required": ["title"],
    "additionalProperties": False,
}


SPEC = ToolSpec(
    name="task_create_many",
    description=(
        "Create several concrete tasks in one project. Use when the user asks to persist an enumerated set of tasks "
        "from the recent conversation. Copy every actual item; never create a task literally named it, them, those, or tasks."
    ),
    parameters=schema(
        {
            "project": {"type": "string", "description": "Project name or id."},
            "tasks": {"type": "array", "items": TASK_ITEM},
        },
        required=("project", "tasks"),
    ),
)


def run(ctx: ToolContext, args: JsonObject) -> ToolResult:
    return run_project_action(SPEC.name, "task_create_many", ctx, args)
