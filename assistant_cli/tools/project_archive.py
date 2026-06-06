from __future__ import annotations

from .core import JsonObject, ToolContext, ToolResult, ToolSpec, schema
from .project_common import run_project_action


SPEC = ToolSpec(
    name="project_archive",
    description="Archive one project. Use only when the user explicitly asks to archive it.",
    parameters=schema(
        {"project": {"type": "string", "description": "Project name or id."}},
        required=("project",),
    ),
)


def run(ctx: ToolContext, args: JsonObject) -> ToolResult:
    return run_project_action(SPEC.name, "project_archive", ctx, args)
