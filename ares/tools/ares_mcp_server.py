"""Local MCP server exposing Ares data analysis, chart, and document tools.

Run as a stdio MCP server:
    python -m ares.tools.ares_mcp_server

Or configure in mcp_servers:
    {
        "ares-tools": {
            "command": "python",
            "args": ["-m", "ares.tools.ares_mcp_server"],
            "transport": "stdio"
        }
    }

Tools exposed:
    - ares_analyze_data   : CSV/TSV/text analysis with pandas
    - ares_generate_chart : Chart generation with matplotlib
    - ares_convert_document : Document conversion with pandoc
    - ares_write_file     : Write files to disk
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

try:
    from mcp.server import Server
    from mcp.server.stdio import stdio_server
    from mcp.types import TextContent, Tool
    _MCP_AVAILABLE = True
except ImportError:
    _MCP_AVAILABLE = False


def _make_text_content(text: str) -> TextContent:
    return TextContent(type="text", text=text)


def _make_error_content(error: str) -> TextContent:
    return TextContent(type="text", text=json.dumps({"ok": False, "error": error}))


def _handle_analyze_data(args: dict[str, Any]) -> str:
    """Handle analyze_data tool call."""
    from ares.tools.data_analysis import analyze_csv, analyze_text

    file_path = args.get("file_path", "")
    if not file_path:
        return json.dumps({"ok": False, "error": "file_path is required"})

    path = Path(file_path).expanduser().resolve()
    if not path.is_file():
        return json.dumps({"ok": False, "error": f"file not found: {path}"})

    ext = path.suffix.lower()
    if ext in (".csv", ".tsv"):
        focus = args.get("focus", "summary")
        generate = args.get("charts", True)
        output_dir = args.get("output_dir")
        result = analyze_csv(path, focus=focus, chart_output_dir=output_dir, generate_charts=generate)
    elif ext in (".txt", ".md", ".rst"):
        result = analyze_text(path)
    else:
        result = analyze_csv(path, focus=args.get("focus", "summary"),
                             chart_output_dir=args.get("output_dir"),
                             generate_charts=args.get("charts", True))
        if not result.get("ok") and "failed to read" in str(result.get("error", "")):
            result = analyze_text(path)

    return json.dumps(result, default=str)


def _handle_generate_chart(args: dict[str, Any]) -> str:
    """Handle generate_chart tool call."""
    import uuid
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return json.dumps({"ok": False, "error": "matplotlib is not installed"})

    chart_type = args.get("chart_type", "line")
    title = args.get("title", "Chart")
    labels = args.get("labels", [])
    values = args.get("values", [])
    width = args.get("width", 1200)
    height = args.get("height", 750)
    output = args.get("output")

    if not labels or not values:
        return json.dumps({"ok": False, "error": "labels and values are required"})

    try:
        values = [float(v) for v in values]
    except (TypeError, ValueError):
        return json.dumps({"ok": False, "error": "values must be numeric"})

    try:
        output_path = Path(output).expanduser() if output else Path(tempfile.gettempdir()) / f"ares-chart-{uuid.uuid4().hex[:12]}.png"
    except Exception as exc:
        return json.dumps({"ok": False, "error": f"invalid output path: {exc}"})

    output_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        figure, axes = plt.subplots(figsize=(width / 150, height / 150), dpi=150)
        if chart_type == "pie":
            axes.pie(values[: len(labels)], labels=labels[: len(values)], autopct="%1.1f%%", startangle=140)
            axes.set_title(title)
        elif chart_type == "bar":
            axes.bar(labels[: len(values)], values[: len(labels)])
            axes.set_title(title)
            axes.set_xlabel("Category")
            axes.set_ylabel("Value")
            axes.grid(True, axis="y", linestyle="--", alpha=0.4)
            if len(labels) <= 20:
                axes.bar_label(axes.containers[0], fmt="%.2g")
        else:
            axes.plot(labels[: len(values)], values[: len(labels)], marker="o")
            axes.set_title(title)
            axes.set_xlabel("Period")
            axes.set_ylabel("Value")
            axes.grid(True, linestyle="--", alpha=0.4)
        figure.tight_layout()
        figure.savefig(output_path)
        plt.close(figure)
    except Exception as exc:
        return json.dumps({"ok": False, "error": f"chart generation failed: {exc}"})

    return json.dumps({
        "ok": True,
        "path": str(output_path),
        "chart_type": chart_type,
        "title": title,
    })


def _handle_convert_document(args: dict[str, Any]) -> str:
    """Handle convert_document tool call."""
    import shutil
    import subprocess

    if not shutil.which("pandoc"):
        return json.dumps({"ok": False, "error": "pandoc is not installed"})

    input_path = Path(args.get("input", "")).expanduser().resolve()
    if not input_path.is_file():
        return json.dumps({"ok": False, "error": f"input file not found: {input_path}"})

    output_format = args.get("output_format", "pdf").lower()
    format_ext = {"pdf": "pdf", "docx": "docx", "html": "html", "latex": "tex", "epub": "epub"}
    ext = format_ext.get(output_format, output_format)

    output = args.get("output")
    if output:
        output_path = Path(output).expanduser().resolve()
    else:
        output_path = input_path.with_suffix(f".{ext}")
    output_path.parent.mkdir(parents=True, exist_ok=True)

    cmd = ["pandoc", str(input_path), "-o", str(output_path)]
    if output_format == "pdf":
        cmd.extend(["--pdf-engine=xelatex"])
    extra = args.get("extra_args")
    if extra:
        cmd.extend(extra.split())

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            return json.dumps({"ok": False, "error": f"pandoc failed: {result.stderr.strip()}"})
    except subprocess.TimeoutExpired:
        return json.dumps({"ok": False, "error": "pandoc timed out after 120s"})

    return json.dumps({
        "ok": True,
        "input": str(input_path),
        "output": str(output_path),
        "format": output_format,
    })


def _handle_write_file(args: dict[str, Any]) -> str:
    """Handle write_file tool call."""
    file_path = args.get("file_path", "")
    content = args.get("content", "")

    if not file_path:
        return json.dumps({"ok": False, "error": "file_path is required"})

    path = Path(file_path).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)

    try:
        path.write_text(content, encoding="utf-8")
    except Exception as exc:
        return json.dumps({"ok": False, "error": f"write failed: {exc}"})

    return json.dumps({"ok": True, "path": str(path), "bytes": len(content.encode("utf-8"))})


# Tool definitions in MCP format
TOOLS = [
    Tool(
        name="ares_analyze_data",
        description="Analyze a CSV, TSV, or text file. Extracts summary statistics, trends, outliers, correlations, and auto-generates charts. Use focus='urgent' or 'patients' to highlight critical cases.",
        inputSchema={
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "Path to the CSV, TSV, or text file."},
                "focus": {"type": "string", "enum": ["summary", "urgent", "patients", "trends", "correlations"], "default": "summary", "description": "Analysis focus."},
                "charts": {"type": "boolean", "default": True, "description": "Generate charts."},
                "output_dir": {"type": "string", "description": "Directory for chart output."},
            },
            "required": ["file_path"],
        },
    ),
    Tool(
        name="ares_generate_chart",
        description="Generate a chart image (line, bar, or pie) from labels and values. Saves to a local PNG file.",
        inputSchema={
            "type": "object",
            "properties": {
                "chart_type": {"type": "string", "enum": ["line", "bar", "pie"], "default": "line"},
                "title": {"type": "string", "description": "Chart title."},
                "labels": {"type": "array", "items": {"type": "string"}, "description": "Category or period labels."},
                "values": {"type": "array", "items": {"type": "number"}, "description": "Numeric values aligned to labels."},
                "output": {"type": "string", "description": "Optional PNG output path."},
                "width": {"type": "integer", "default": 1200},
                "height": {"type": "integer", "default": 750},
            },
            "required": ["chart_type", "title", "labels", "values"],
        },
    ),
    Tool(
        name="ares_convert_document",
        description="Convert a document from one format to another using pandoc. Supports markdown to PDF, DOCX, HTML, LaTeX, EPUB.",
        inputSchema={
            "type": "object",
            "properties": {
                "input": {"type": "string", "description": "Path to the input file."},
                "output_format": {"type": "string", "enum": ["pdf", "docx", "html", "latex", "epub"], "description": "Target format."},
                "output": {"type": "string", "description": "Optional output path."},
                "extra_args": {"type": "string", "description": "Optional extra pandoc arguments."},
            },
            "required": ["input", "output_format"],
        },
    ),
    Tool(
        name="ares_write_file",
        description="Write text content to a file. Creates parent directories automatically.",
        inputSchema={
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "Path to write to."},
                "content": {"type": "string", "description": "Text content to write."},
            },
            "required": ["file_path", "content"],
        },
    ),
]

TOOL_HANDLERS = {
    "ares_analyze_data": _handle_analyze_data,
    "ares_generate_chart": _handle_generate_chart,
    "ares_convert_document": _handle_convert_document,
    "ares_write_file": _handle_write_file,
}


async def main():
    """Run the Ares tools MCP server over stdio."""
    if not _MCP_AVAILABLE:
        print("ERROR: mcp package is not installed. Install with: pip install mcp", file=sys.stderr)
        sys.exit(1)

    server = Server("ares-tools")

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        return TOOLS

    @server.call_tool()
    async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
        handler = TOOL_HANDLERS.get(name)
        if not handler:
            return [_make_error_content(f"unknown tool: {name}")]
        try:
            result = handler(arguments)
            return [_make_text_content(result)]
        except Exception as exc:
            return [_make_error_content(f"tool error: {exc}")]

    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
