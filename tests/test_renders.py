"""Tests for tool result renderers."""

import json

from rich.console import Console

from ares.tools.renders import (
    get_renderer,
    render_directory,
    render_command_result,
    render_file_content,
    render_file_operation,
    render_generic_tool,
    render_image_result,
    render_json_status,
    render_memory_result,
    render_search_results,
    render_skills_result,
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


class TestRenderMemoryResult:
    def test_renders_memory_search_table(self):
        content = "Found 1 memories:\n- #42 [profile, importance=0.9] User likes simple tables"
        output = render_to_text(render_memory_result(content))
        assert "Memory Search" in output
        assert "42" in output
        assert "profile" in output
        assert "User likes simple tables" in output

    def test_renders_memory_action_table(self):
        output = render_to_text(render_memory_result("Stored memory #7: User prefers short answers"))
        assert "Stored" in output
        assert "7" in output
        assert "short answers" in output


class TestRenderSkillsResult:
    def test_renders_skill_and_category_tables(self):
        content = (
            "Available skills (2):\n"
            "- code-review [coding]: Review code safely\n"
            "- daily-planner [productivity]: Plan the day\n"
            "Categories: coding (1), productivity (1)"
        )
        output = render_to_text(render_skills_result(content))
        assert "Available Skills" in output
        assert "code-review" in output
        assert "productivity" in output
        assert "Categories" in output


class TestRenderCommandResult:
    def test_renders_status_and_stdout(self):
        content = "Exit code: 0\nSummary: status=ok; stdout_lines=1; stderr_lines=0\n--- stdout ---\nhello"
        output = render_to_text(render_command_result(content))
        assert "Exit code" in output
        assert "ok" in output
        assert "STDOUT" in output
        assert "hello" in output


class TestRenderJsonStatus:
    def test_renders_phone_bridge_table(self):
        content = json.dumps({
            "kdeconnect": {"ok": True, "device_id": "phone-1"},
            "adb": {"ok": False, "error": "not connected"},
        })
        output = render_to_text(render_json_status(content))
        assert "Phone Bridge" in output
        assert "KDE Connect" in output
        assert "PASS" in output
        assert "FAIL" in output


class TestRenderFileOperation:
    def test_renders_diff_syntax(self):
        content = "Edited app.py\n--- app.py\n+++ app.py\n@@ -1 +1 @@\n-old\n+new"
        output = render_to_text(render_file_operation(content))
        assert "File Diff" in output
        assert "Edited app.py" in output
        assert "new" in output


class TestRenderImageResult:
    def test_renders_image_metadata(self):
        content = "Format: PNG (Portable network graphics)\nDimensions: 100x50\nMode: RGB\nSize: 3.2 KB"
        output = render_to_text(render_image_result(content))
        assert "Format" in output
        assert "PNG" in output
        assert "Dimensions" in output


class TestGetRenderer:
    def test_known_tool_routes_correctly(self):
        assert get_renderer("web_search") is render_web_search
        assert get_renderer("read_file") is render_file_content
        assert get_renderer("search_files") is render_search_results
        assert get_renderer("list_directory") is render_directory
        assert get_renderer("store_memory") is render_memory_result
        assert get_renderer("run_command") is render_command_result

    def test_unknown_tool_returns_default(self):
        assert get_renderer("random_thing") is render_generic_tool
