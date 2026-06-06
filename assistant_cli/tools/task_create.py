from __future__ import annotations

from .core import JsonObject, ToolContext, ToolResult, ToolSpec, schema
from .project_common import run_project_action


TASK_FIELDS = {
    "project": {"type": "string", "description": "Project name or id."},
    "title": {"type": "string", "description": "Concrete task title."},
    "description": {"type": "string"},
    "status": {
        "type": "string",
        "enum": ["pending", "in_progress", "blocked", "done"],
        "default": "pending",
    },
    "priority": {"type": "string", "enum": ["low", "normal", "high", "urgent"], "default": "normal"},
    "due": {"type": "string", "description": "Natural or ISO due date/time."},
    "tags": {"type": "array", "items": {"type": "string"}},
    "recurrence": {"type": "string", "description": "Recurrence such as weekly or every Friday."},
}


SPEC = ToolSpec(
    name="task_create",
    description=(
        "Create one concrete persistent task in a project. "
        "Do not use for project descriptions, brainstorming topics, pronouns, or vague placeholders."
    ),
    parameters=schema(TASK_FIELDS, required=("project", "title")),
)


def run(ctx: ToolContext, args: JsonObject) -> ToolResult:
    return run_project_action(SPEC.name, "task_create", ctx, args)
