from __future__ import annotations

from .core import JsonObject, ToolContext, ToolResult, ToolSpec, schema
from .project_common import run_project_action


SPEC = ToolSpec(
    name="task_complete_all",
    description="Mark every open task in one project done immediately.",
    parameters=schema(
        {"project": {"type": "string", "description": "Project name or id."}},
        required=("project",),
    ),
)


def run(ctx: ToolContext, args: JsonObject) -> ToolResult:
    return run_project_action(SPEC.name, "task_complete_all", ctx, args)
