from __future__ import annotations

import base64

from .args import str_arg
from .core import JsonObject, ToolContext, ToolResult, ToolSpec, ok, schema


SPEC = ToolSpec(
    name="base64_encode",
    description="Encode text as base64.",
    parameters=schema({"text": {"type": "string"}}, required=("text",)),
    examples=("base64_encode text=friday",),
)


def run(ctx: ToolContext, args: JsonObject) -> ToolResult:
    text = str_arg(args, "text")
    encoded = base64.b64encode(text.encode("utf-8")).decode("ascii")
    return ok("base64_encode", encoded, {"encoded": encoded})
