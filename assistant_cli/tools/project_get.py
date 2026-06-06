from __future__ import annotations

from .core import JsonObject, ToolContext, ToolResult, ToolSpec, schema
from .project_common import run_project_action


SPEC = ToolSpec(
    name="project_get",
    description="Read one persistent project, including its task counts and current open tasks.",
    parameters=schema(
        {"project": {"type": "string", "description": "Project name or project id."}},
        required=("project",),
    ),
)


def run(ctx: ToolContext, args: JsonObject) -> ToolResult:
    return run_project_action(SPEC.name, "summary", ctx, args)
