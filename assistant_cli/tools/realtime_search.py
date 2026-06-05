from __future__ import annotations

from .args import int_arg, str_arg
from .core import JsonObject, ToolContext, ToolResult, ToolSpec, fail, ok, schema


SPEC = ToolSpec(
    name="realtime_search",
    description="Search current web results with Tavily. Requires TAVILY_API_KEY.",
    parameters=schema(
        {
            "query": {"type": "string", "description": "Search query."},
            "max_results": {"type": "integer", "minimum": 1, "maximum": 10, "default": 5},
            "include_answer": {"type": "boolean", "default": True},
        },
        required=("query",),
    ),
    examples=("realtime_search query=\"latest NVIDIA NIM models\" max_results=5",),
)


def run(ctx: ToolContext, args: JsonObject) -> ToolResult:
    query = str_arg(args, "query")
    if not query:
        return fail("realtime_search", "query is required")
    if not ctx.settings.tavily_api_key:
        return fail(
            "realtime_search",
            "TAVILY_API_KEY is missing. Add it to .env to enable realtime web search.",
            {"missing": "TAVILY_API_KEY"},
        )

    max_results = int_arg(args, "max_results", 5, 1, 10)
    include_answer = bool(args.get("include_answer", True))
    response = ctx.http.post(
        "https://api.tavily.com/search",
        json={
            "api_key": ctx.settings.tavily_api_key,
            "query": query,
            "search_depth": "advanced",
            "include_answer": include_answer,
            "max_results": max_results,
        },
    )
    response.raise_for_status()
    data = response.json()
    rows = data.get("results") if isinstance(data, dict) else []
    if not isinstance(rows, list):
        rows = []
    lines = [f"Tavily results for: {query}"]
    answer = str(data.get("answer") or "").strip() if isinstance(data, dict) else ""
    if answer:
        lines.append("")
        lines.append(answer)
    for index, item in enumerate(rows[:max_results], 1):
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "Untitled").strip()
        url = str(item.get("url") or "").strip()
        content = str(item.get("content") or "").strip()
        score = item.get("score")
        meta = f" score={score}" if score is not None else ""
        lines.append(f"{index}. {title}{meta}")
        if content:
            lines.append(f"   {content[:400]}")
        if url:
            lines.append(f"   {url}")
    return ok("realtime_search", "\n".join(lines), {"query": query, "results": rows, "answer": answer})
