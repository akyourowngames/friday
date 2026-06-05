from __future__ import annotations

from .args import int_arg, relative_path, safe_path, str_arg
from .core import JsonObject, ToolContext, ToolResult, ToolSpec, fail, ok, schema


SPEC = ToolSpec(
    name="file_list",
    description="List files inside the current workspace.",
    parameters=schema(
        {
            "path": {"type": "string", "default": "."},
            "max_entries": {"type": "integer", "minimum": 1, "maximum": 500, "default": 100},
        }
    ),
    examples=("file_list path=. max_entries=20",),
)


def run(ctx: ToolContext, args: JsonObject) -> ToolResult:
    target = safe_path(ctx.workspace_root, str_arg(args, "path", "."))
    max_entries = int_arg(args, "max_entries", 100, 1, 500)
    if not target.exists():
        return fail("file_list", f"Path does not exist: {relative_path(ctx.workspace_root, target)}")
    if not target.is_dir():
        return fail("file_list", f"Path is not a directory: {relative_path(ctx.workspace_root, target)}")
    entries = sorted(target.iterdir(), key=lambda path: (path.is_file(), path.name.lower()))[:max_entries]
    rows = [
        {
            "path": relative_path(ctx.workspace_root, path),
            "type": "dir" if path.is_dir() else "file",
            "size": path.stat().st_size,
        }
        for path in entries
    ]
    lines = [f"{len(rows)} entries under {relative_path(ctx.workspace_root, target) or '.'}"]
    for row in rows:
        marker = "DIR " if row["type"] == "dir" else "FILE"
        lines.append(f"{marker} {row['path']}")
    return ok("file_list", "\n".join(lines), {"entries": rows, "truncated": len(rows) == max_entries})
