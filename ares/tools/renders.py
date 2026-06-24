"""Rich renderers for tool results."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any

from rich.console import Group
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text
from rich.tree import Tree


def render_web_search(content: str) -> Any:
    """Render structured web search results as summary plus numbered cards."""
    try:
        payload = json.loads(content)
    except (TypeError, json.JSONDecodeError):
        return Panel(Text(content), title="Web Search", border_style="bright_green")

    if isinstance(payload, list):
        payload = {"query": "", "provider": "web", "summary": "", "results": payload, "errors": []}

    results = payload.get("results", [])
    query = payload.get("query", "")
    provider = payload.get("provider", "")
    summary = payload.get("summary", "")
    errors = payload.get("errors", [])

    title_parts = ["Web Search"]
    if provider:
        title_parts.append(provider)
    if query:
        title_parts.append(f'"{query}"')

    renderables: list[Any] = []
    if summary and summary != "No summary available.":
        renderables.append(Text("Summary", style="bold bright_green"))
        renderables.append(Text(summary, style="default"))
        renderables.append(Text())

    if not results:
        renderables.append(Text("No results found.", style="dim"))
    else:
        for index, result in enumerate(results, 1):
            title = str(result.get("title") or "Untitled")
            url = str(result.get("url") or "")
            snippet = re.sub(r"\s+", " ", str(result.get("snippet") or "")).strip()
            if len(snippet) > 160:
                snippet = snippet[:157].rstrip() + "..."

            heading = Text()
            heading.append(f"{index:>2}  ", style="bold cyan")
            heading.append(title, style="bold")
            renderables.append(heading)
            if url:
                renderables.append(Text(f"    {url}", style="dim underline"))
            if snippet:
                renderables.append(Text(f"    {snippet}", style="dim"))
            if index < len(results):
                renderables.append(Text())

    if errors:
        renderables.append(Text())
        renderables.append(Text("Warnings: " + "; ".join(str(e) for e in errors[:3]), style="yellow"))

    return Panel(
        Group(*renderables),
        title=" | ".join(title_parts),
        border_style="bright_green",
        padding=(0, 1),
    )


def _language_for_path(path: str) -> str:
    suffix = path.lower().rsplit(".", 1)[-1] if "." in path else ""
    return {
        "py": "python",
        "js": "javascript",
        "ts": "typescript",
        "tsx": "tsx",
        "jsx": "jsx",
        "rs": "rust",
        "go": "go",
        "java": "java",
        "c": "c",
        "h": "c",
        "cpp": "cpp",
        "hpp": "cpp",
        "rb": "ruby",
        "sh": "bash",
        "bash": "bash",
        "json": "json",
        "yaml": "yaml",
        "yml": "yaml",
        "toml": "toml",
        "md": "markdown",
        "html": "html",
        "css": "css",
        "sql": "sql",
        "xml": "xml",
    }.get(suffix, "text")


def render_file_content(content: str) -> Any:
    """Render read_file output as syntax highlighted content."""
    if "binary file" in content.lower() or "cannot display" in content.lower():
        return Panel(Text(content, style="yellow"), title="File", border_style="yellow")

    header_match = re.search(r"\[File: (.+?) \((\d+) lines total\)\]", content)
    title = "File Content"
    lang = "text"
    if header_match:
        title = f"{header_match.group(1)} ({header_match.group(2)} lines)"
        lang = _language_for_path(header_match.group(1))

    code_lines = []
    notes = []
    for line in content.splitlines():
        if line.startswith("[File:"):
            continue
        if line.startswith("(") and line.endswith(")"):
            notes.append(line)
            continue
        match = re.match(r"^\s*\d+\t(.*)$", line)
        if match:
            code_lines.append(match.group(1))
        elif line.strip():
            code_lines.append(line)

    if not code_lines:
        return Panel(Text(content), title=title, border_style="bright_blue")

    shown_code = "\n".join(code_lines)
    syntax = Syntax(shown_code, lang, theme="monokai", line_numbers=True, word_wrap=True)
    renderables: list[Any] = [syntax]
    if notes:
        renderables.append(Text("  " + " | ".join(notes), style="dim"))

    return Panel(Group(*renderables), title=title, border_style="bright_blue", padding=(0, 1))


def render_search_results(content: str) -> Any:
    """Render search_files output with match type badges."""
    if "no results" in content.lower() or "found 0" in content.lower():
        return Panel(Text("No matching files found.", style="dim"), title="File Search", border_style="bright_yellow")

    table = Table(show_header=True, header_style="bold", border_style="bright_yellow")
    table.add_column("Type", width=10)
    table.add_column("Location", ratio=3)
    table.add_column("Excerpt", ratio=5)

    lines = content.splitlines()
    pending: tuple[str, str] | None = None
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[content match]"):
            pending = ("content", stripped.removeprefix("[content match]").strip())
        elif stripped.startswith("[name match]"):
            pending = ("name", stripped.removeprefix("[name match]").strip())
            table.add_row(Text("name", style="bold magenta"), Text(pending[1], style="bold"), "")
            pending = None
        elif pending and stripped and not stripped.startswith("Found "):
            match_type, location = pending
            style = "cyan" if match_type == "content" else "magenta"
            table.add_row(Text(match_type, style=f"bold {style}"), Text(location, style="bold"), Text(stripped, style="dim"))
            pending = None

    if table.row_count == 0:
        return Panel(Text(content), title="File Search", border_style="bright_yellow")
    return Panel(table, title="File Search Results", border_style="bright_yellow", padding=(0, 1))


def render_directory(content: str) -> Any:
    """Render list_directory output as a tree."""
    lines = [line.strip() for line in content.splitlines() if line.strip()]
    title = "Directory"
    if lines and lines[0].startswith("[Directory:"):
        title = lines[0].removeprefix("[Directory:").removesuffix("]").strip()
        lines = lines[1:]

    tree = Tree(title, guide_style="dim")
    added = 0
    footer = ""
    for line in lines:
        if line.startswith("..."):
            footer = line
            continue
        if line.startswith("[dir]"):
            tree.add(Text(line.removeprefix("[dir]").strip(), style="bold cyan"))
            added += 1
        elif line.startswith("[file]"):
            tree.add(Text(line.removeprefix("[file]").strip(), style="default"))
            added += 1

    if footer:
        tree.add(Text(footer, style="dim"))
    if added == 0 and not footer:
        tree.add(Text("Empty directory.", style="dim"))
    return Panel(tree, title=title, border_style="bright_magenta", padding=(0, 1))


def render_generic_tool(content: str) -> Any:
    """Render generic tool results compactly."""
    clean = content.strip()
    if not clean:
        return Text("  *  No result", style="dim")
    if "\n" not in clean and len(clean) < 120:
        return Text(f"  *  {clean}", style="dim")
    return Panel(Text(clean), title="Tool Result", border_style="dim", padding=(0, 1))


RENDERERS: dict[str, Callable[[str], Any]] = {
    "web_search": render_web_search,
    "read_file": render_file_content,
    "search_files": render_search_results,
    "list_directory": render_directory,
}
DEFAULT_RENDERER = render_generic_tool


def get_renderer(tool_name: str) -> Callable[[str], Any]:
    """Return the renderer for a tool name."""
    return RENDERERS.get(tool_name, DEFAULT_RENDERER)
