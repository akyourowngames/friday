from __future__ import annotations

from .core import JsonObject, ToolContext, ToolResult, ToolSpec, schema
from .project_common import run_project_action


SPEC = ToolSpec(
    name="project_update",
    description=(
        "Update a persistent project's name, purpose/description, or status. "
        "Use this when the user explains what a project is or what it is for; do not create a task for project metadata."
    ),
    parameters=schema(
        {
            "project": {"type": "string", "description": "Existing project name or id."},
            "name": {"type": "string", "description": "New project name."},
            "description": {"type": "string", "description": "Project purpose or description."},
            "status": {
                "type": "string",
                "enum": ["pending", "in_progress", "blocked", "done", "archived"],
            },
        },
        required=("project",),
    ),
)


def run(ctx: ToolContext, args: JsonObject) -> ToolResult:
    return run_project_action(SPEC.name, "project_update", ctx, args)
