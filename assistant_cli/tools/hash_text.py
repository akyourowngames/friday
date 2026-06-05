from __future__ import annotations

import hashlib

from .args import str_arg
from .core import JsonObject, ToolContext, ToolResult, ToolSpec, fail, ok, schema


SPEC = ToolSpec(
    name="hash_text",
    description="Hash text with sha256, sha1, or md5.",
    parameters=schema(
        {
            "text": {"type": "string"},
            "algorithm": {"type": "string", "enum": ["sha256", "sha1", "md5"], "default": "sha256"},
        },
        required=("text",),
    ),
    examples=("hash_text text=friday algorithm=sha256",),
)


def run(ctx: ToolContext, args: JsonObject) -> ToolResult:
    text = str_arg(args, "text")
    algorithm = str_arg(args, "algorithm", "sha256").lower()
    if algorithm not in {"sha256", "sha1", "md5"}:
        return fail("hash_text", "algorithm must be sha256, sha1, or md5")
    digest = hashlib.new(algorithm, text.encode("utf-8")).hexdigest()
    return ok("hash_text", digest, {"algorithm": algorithm, "digest": digest})
