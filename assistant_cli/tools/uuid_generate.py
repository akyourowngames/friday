from __future__ import annotations

import uuid

from .args import int_arg
from .core import JsonObject, ToolContext, ToolResult, ToolSpec, ok, schema


SPEC = ToolSpec(
    name="uuid_generate",
    description="Generate UUID values.",
    parameters=schema({"count": {"type": "integer", "minimum": 1, "maximum": 20, "default": 1}}),
    examples=("uuid_generate count=3",),
)


def run(ctx: ToolContext, args: JsonObject) -> ToolResult:
    count = int_arg(args, "count", 1, 1, 20)
    values = [str(uuid.uuid4()) for _ in range(count)]
    return ok("uuid_generate", "\n".join(values), {"values": values})
