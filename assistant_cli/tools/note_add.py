from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

from .args import relative_path, str_arg
from .core import JsonObject, ToolContext, ToolResult, ToolSpec, fail, ok, schema
from .notes_common import notes_path


SPEC = ToolSpec(
    name="note_add",
    description="Append a local note to storage/tool_notes.jsonl.",
    parameters=schema(
        {
            "text": {"type": "string"},
            "tags": {"type": "array", "items": {"type": "string"}},
        },
        required=("text",),
    ),
    examples=("note_add text=\"ship tool split\"",),
)


def run(ctx: ToolContext, args: JsonObject) -> ToolResult:
    text = str_arg(args, "text")
    if not text:
        return fail("note_add", "text is required")
    tags_value = args.get("tags", [])
    tags = [str(tag).strip() for tag in tags_value if str(tag).strip()] if isinstance(tags_value, list) else []
    path = notes_path(ctx)
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "id": str(uuid.uuid4()),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "text": text,
        "tags": tags,
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    return ok("note_add", f"Saved note {row['id']}", {"note": row, "path": relative_path(ctx.workspace_root, path)})
