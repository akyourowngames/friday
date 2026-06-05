from __future__ import annotations

import secrets
import string

from .args import int_arg
from .core import JsonObject, ToolContext, ToolResult, ToolSpec, ok, schema


SPEC = ToolSpec(
    name="password_generate",
    description="Generate a local random password.",
    parameters=schema(
        {
            "length": {"type": "integer", "minimum": 8, "maximum": 128, "default": 20},
            "symbols": {"type": "boolean", "default": True},
        }
    ),
    examples=("password_generate length=20 symbols=true",),
)


def run(ctx: ToolContext, args: JsonObject) -> ToolResult:
    length = int_arg(args, "length", 20, 8, 128)
    include_symbols = bool(args.get("symbols", True))
    alphabet = string.ascii_letters + string.digits
    if include_symbols:
        alphabet += "!@#$%^&*_-+=?"
    password = "".join(secrets.choice(alphabet) for _ in range(length))
    return ok("password_generate", password, {"length": length, "symbols": include_symbols})
