from __future__ import annotations

import random

from .args import int_arg
from .core import JsonObject, ToolContext, ToolResult, ToolSpec, ok, schema


SPEC = ToolSpec(
    name="random_number",
    description="Generate random integers.",
    parameters=schema(
        {
            "minimum": {"type": "integer", "default": 1},
            "maximum": {"type": "integer", "default": 100},
            "count": {"type": "integer", "minimum": 1, "maximum": 50, "default": 1},
        }
    ),
    examples=("random_number minimum=1 maximum=10 count=3",),
)


def run(ctx: ToolContext, args: JsonObject) -> ToolResult:
    minimum = int_arg(args, "minimum", 1, -1_000_000_000, 1_000_000_000)
    maximum = int_arg(args, "maximum", 100, -1_000_000_000, 1_000_000_000)
    count = int_arg(args, "count", 1, 1, 50)
    if minimum > maximum:
        minimum, maximum = maximum, minimum
    values = [random.randint(minimum, maximum) for _ in range(count)]
    return ok("random_number", ", ".join(str(value) for value in values), {"values": values})
