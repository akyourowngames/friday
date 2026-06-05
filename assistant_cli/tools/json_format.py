from __future__ import annotations

import json

from .args import str_arg
from .core import JsonObject, ToolContext, ToolResult, ToolSpec, ok, schema


SPEC = ToolSpec(
    name="json_format",
    description="Validate and pretty-print JSON.",
    parameters=schema({"json_text": {"type": "string"}}, required=("json_text",)),
    examples=("json_format json_text='{\"b\":2,\"a\":1}'",),
)


def run(ctx: ToolContext, args: JsonObject) -> ToolResult:
    raw = str_arg(args, "json_text")
    value = json.loads(raw)
    formatted = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)
    return ok("json_format", formatted, {"value": value})
