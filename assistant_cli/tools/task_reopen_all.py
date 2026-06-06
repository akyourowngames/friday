from __future__ import annotations

from .core import JsonObject, ToolContext, ToolResult, ToolSpec, schema
from .project_common import run_project_action


SPEC = ToolSpec(
    name="task_reopen_all",
    description="Reopen every non-archived task in one project as pending.",
    parameters=schema(
        {"project": {"type": "string", "description": "Project name or id."}},
        required=("project",),
    ),
)


def run(ctx: ToolContext, args: JsonObject) -> ToolResult:
    return run_project_action(SPEC.name, "task_pending_all", ctx, args)
