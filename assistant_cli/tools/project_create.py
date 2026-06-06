from __future__ import annotations

from .core import JsonObject, ToolContext, ToolResult, ToolSpec, schema
from .project_common import run_project_action


SPEC = ToolSpec(
    name="project_create",
    description="Create a persistent project. Use only when the user asks to create or start a project.",
    parameters=schema(
        {
            "name": {"type": "string", "description": "Project name."},
            "description": {"type": "string", "description": "Project purpose or description."},
            "status": {
                "type": "string",
                "enum": ["pending", "in_progress", "blocked", "done"],
                "default": "in_progress",
            },
        },
        required=("name",),
    ),
)


def run(ctx: ToolContext, args: JsonObject) -> ToolResult:
    return run_project_action(SPEC.name, "project_create", ctx, args)
