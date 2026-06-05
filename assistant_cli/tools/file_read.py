from __future__ import annotations

from .args import int_arg, relative_path, safe_path, str_arg
from .core import JsonObject, ToolContext, ToolResult, ToolSpec, fail, ok, schema


SPEC = ToolSpec(
    name="file_read",
    description="Read a UTF-8 text file inside the current workspace.",
    parameters=schema(
        {
            "path": {"type": "string"},
            "max_chars": {"type": "integer", "minimum": 200, "maximum": 20000, "default": 4000},
        },
        required=("path",),
    ),
    examples=("file_read path=README.md max_chars=1200",),
)


def run(ctx: ToolContext, args: JsonObject) -> ToolResult:
    target = safe_path(ctx.workspace_root, str_arg(args, "path"))
    max_chars = int_arg(args, "max_chars", 4000, 200, 20000)
    if not target.exists():
        return fail("file_read", f"File does not exist: {relative_path(ctx.workspace_root, target)}")
    if not target.is_file():
        return fail("file_read", f"Path is not a file: {relative_path(ctx.workspace_root, target)}")
    text = target.read_text(encoding="utf-8", errors="replace")
    truncated = len(text) > max_chars
    shown = text[:max_chars] + ("\n... [truncated]" if truncated else "")
    return ok("file_read", shown, {"path": relative_path(ctx.workspace_root, target), "truncated": truncated})
