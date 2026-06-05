from __future__ import annotations

import json

from .args import int_arg
from .core import JsonObject, ToolContext, ToolResult, ToolSpec, ok, schema
from .notes_common import notes_path


SPEC = ToolSpec(
    name="note_list",
    description="List recent local notes from storage/tool_notes.jsonl.",
    parameters=schema({"limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10}}),
    examples=("note_list limit=5",),
)


def run(ctx: ToolContext, args: JsonObject) -> ToolResult:
    limit = int_arg(args, "limit", 10, 1, 50)
    path = notes_path(ctx)
    if not path.exists():
        return ok("note_list", "No notes saved yet.", {"notes": []})
    rows: list[JsonObject] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            rows.append(item)
    notes = rows[-limit:]
    lines = [f"{len(notes)} recent notes"]
    for row in notes:
        tags = ", ".join(row.get("tags", [])) if isinstance(row.get("tags"), list) else ""
        suffix = f" [{tags}]" if tags else ""
        lines.append(f"- {row.get('created_at', '')}: {row.get('text', '')}{suffix}")
    return ok("note_list", "\n".join(lines), {"notes": notes})
