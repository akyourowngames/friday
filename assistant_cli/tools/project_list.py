from __future__ import annotations

from .core import JsonObject, ToolContext, ToolResult, ToolSpec, schema
from .project_common import run_project_action


SPEC = ToolSpec(
    name="project_list",
    description="List persistent projects and their open/done task counts.",
    parameters=schema(
        {
            "status": {
                "type": "string",
                "enum": ["pending", "in_progress", "blocked", "done", "archived"],
            },
            "include_archived": {"type": "boolean", "default": False},
            "limit": {"type": "integer", "default": 20},
        }
    ),
)


def run(ctx: ToolContext, args: JsonObject) -> ToolResult:
    return run_project_action(SPEC.name, "project_list", ctx, args)
