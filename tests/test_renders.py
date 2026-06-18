"""Tests for tool result renderers."""

import json

from rich.console import Console

from ares.renders import (
    get_renderer,
    render_directory,
    render_file_content,
    render_generic_tool,
    render_search_results,
    render_web_search,
)


def render_to_text(renderable) -> str:
    console = Console(width=100, record=True, force_terminal=False)
    console.print(renderable)
    return console.export_text()


class TestRenderWebSearch:
    def test_renders_summary_and_numbered_results(self):
        content = json.dumps({
            "query": "python",
            "provider": "tavily",
            "summary": "Python is a programming language.",
            "results": [
                {"title": "Python Docs", "url": "https://docs.python.org", "snippet": "Official docs"},
                {"title": "Real Python", "url": "https://realpython.com", "snippet": "Tutorials"},
            ],
            "errors": [],
        })
        output = render_to_text(render_web_search(content))
        assert "Python is a programming language." in output
        assert "Python Docs" in output
        assert "https://docs.python.org" in output
        assert " 1" in output
        assert " 2" in output

    def test_handles_invalid_json(self):
        output = render_to_text(render_web_search("not json at all"))
        assert "not json at all" in output

    def test_handles_empty_results(self):
        output = render_to_text(render_web_search(json.dumps({"results": []})))
        assert "no results" in output.lower()


class TestRenderFileContent:
    def test_renders_with_line_numbers(self):
        content = "[File: test.py (3 lines total)]\n     1\tline one\n     2\tline two\n     3\tline three"
        output = render_to_text(render_file_content(content))
        assert "test.py" in output
        assert "line one" in output

    def test_detects_binary_file(self):
        output = render_to_text(render_file_content("Binary file - cannot display content: image.png"))
        assert "binary" in output.lower()


class TestRenderSearchResults:
    def test_renders_file_matches(self):
        content = "Found 2 file result(s):\n[content match] src/app.py:42\n  import os\n[name match] tests/test_app.py"
        output = render_to_text(render_search_results(content))
        assert "app.py" in output
        assert "content" in output
        assert "test_app.py" in output


class TestRenderDirectory:
    def test_renders_directory_listing(self):
        content = "[Directory: ~/project]\n  [dir]  src/\n  [file] main.py  1.2KB\n  [file] README.md  3.4KB"
        output = render_to_text(render_directory(content))
        assert "main.py" in output
        assert "1.2KB" in output
        assert "src" in output


class TestRenderGenericTool:
    def test_renders_simple_message(self):
        output = render_to_text(render_generic_tool("Stored memory #42: User likes blue"))
        assert "Stored memory #42" in output

    def test_renders_multiline_in_panel(self):
        output = render_to_text(render_generic_tool("Found 2 memories:\n- Hello\n- World"))
        assert "Hello" in output


class TestGetRenderer:
    def test_known_tool_routes_correctly(self):
        assert get_renderer("web_search") is render_web_search
        assert get_renderer("read_file") is render_file_content
        assert get_renderer("search_files") is render_search_results
        assert get_renderer("list_directory") is render_directory

    def test_unknown_tool_returns_default(self):
        assert get_renderer("store_memory") is render_generic_tool
        assert get_renderer("random_thing") is render_generic_tool
