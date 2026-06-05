from __future__ import annotations

import base64

from .args import str_arg
from .core import JsonObject, ToolContext, ToolResult, ToolSpec, ok, schema


SPEC = ToolSpec(
    name="base64_decode",
    description="Decode base64 into text.",
    parameters=schema({"text": {"type": "string"}}, required=("text",)),
    examples=("base64_decode text=ZnJpZGF5",),
)


def run(ctx: ToolContext, args: JsonObject) -> ToolResult:
    text = str_arg(args, "text")
    decoded = base64.b64decode(text.encode("ascii"), validate=True).decode("utf-8")
    return ok("base64_decode", decoded, {"decoded": decoded})
