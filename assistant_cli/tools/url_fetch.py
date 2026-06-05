from __future__ import annotations

from urllib.parse import urlparse

from .args import int_arg, str_arg
from .core import JsonObject, ToolContext, ToolResult, ToolSpec, fail, ok, schema


SPEC = ToolSpec(
    name="url_fetch",
    description="Fetch text from an HTTP or HTTPS URL.",
    parameters=schema(
        {
            "url": {"type": "string"},
            "max_chars": {"type": "integer", "minimum": 200, "maximum": 20000, "default": 4000},
        },
        required=("url",),
    ),
    examples=("url_fetch url=https://example.com max_chars=1000",),
)


def run(ctx: ToolContext, args: JsonObject) -> ToolResult:
    url = str_arg(args, "url")
    max_chars = int_arg(args, "max_chars", 4000, 200, 20000)
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return fail("url_fetch", "Only http and https URLs are allowed.")
    response = ctx.http.get(url)
    response.raise_for_status()
    content_type = response.headers.get("content-type", "")
    text = response.text[:max_chars]
    truncated = len(response.text) > max_chars
    if truncated:
        text += "\n... [truncated]"
    return ok(
        "url_fetch",
        text,
        {"url": url, "status_code": response.status_code, "content_type": content_type, "truncated": truncated},
    )
