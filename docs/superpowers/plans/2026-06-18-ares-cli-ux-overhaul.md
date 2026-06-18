# Ares CLI UX Overhaul — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rebuild the CLI rendering pipeline so each tool type gets its own rich visual treatment — web search as numbered cards, files as syntax-highlighted code blocks, directories as trees, with tool-specific status indicators during execution.

**Architecture:** Create a `renders.py` module with a Rich renderer per tool type. The CLI routes tool results to the correct renderer via a registry. Agent yields tool names in tokens. Status indicators shown during tool execution using Rich's `Live` display.

**Tech Stack:** Python 3.11+, Rich (panels, syntax, tables, trees, markdown), existing Ares modules.

**Implementation status:** Complete. Added `ares/renders.py`, named tool tokens, CLI renderer routing, tool-specific live statuses, renderer tests, agent token tests, and CLI routing tests. Verified with the full pytest suite.

---

## File Structure

| File | Action | Responsibility |
|------|--------|----------------|
| `ares/renders.py` | **Create** | All renderers: web search cards, file syntax, search results, directory tree, default |
| `ares/agent.py:185-186` | Modify | Change token format from `[tool:{content}]` to `[tool:{tool_name}:{content}]` |
| `ares/cli.py:329-383` | Modify | Route tokens to renderers, add tool status indicators, update _process_input |
| `tests/test_renders.py` | **Create** | Unit tests for each renderer |

---

## Task 1: Create renderer module with web search renderer

**Files:**
- Create: `ares/renders.py`
- Create: `tests/test_renders.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_renders.py`:

```python
"""Tests for tool result renderers."""

import json

from rich.console import Console
from rich.text import Text

from ares.renders import (
    render_web_search,
    render_file_content,
    render_search_results,
    render_directory,
    render_generic_tool,
    get_renderer,
)


class TestRenderWebSearch:
    def test_renders_numbered_results(self):
        """Web search results show numbered items with title, URL, snippet."""
        content = json.dumps([
            {"title": "Python Docs", "url": "https://docs.python.org", "snippet": "Official docs"},
            {"title": "Real Python", "url": "https://realpython.com", "snippet": "Tutorials"},
        ])
        result = render_web_search(content)
        # Result should be a Rich renderable (Panel)
        console = Console(width=80)
        output = console.export_text(result)
        assert "Python Docs" in output
        assert "https://docs.python.org" in output
        assert "1" in output
        assert "2" in output

    def test_handles_invalid_json(self):
        """Invalid JSON falls back to generic rendering."""
        result = render_web_search("not json at all")
        console = Console(width=80)
        output = console.export_text(result)
        assert "not json at all" in output

    def test_handles_empty_results(self):
        """Empty results list shows 'No results' message."""
        result = render_web_search(json.dumps([]))
        console = Console(width=80)
        output = console.export_text(result)
        assert "no results" in output.lower()


class TestRenderFileContent:
    def test_renders_with_line_numbers(self):
        """File content shows line numbers."""
        content = "[File: test.py (3 lines total)]\n1\tline one\n2\tline two\n3\tline three"
        result = render_file_content(content)
        console = Console(width=80)
        output = console.export_text(result)
        assert "test.py" in output
        assert "line one" in output

    def test_detects_binary_file(self):
        """Binary file message is detected and rendered differently."""
        content = "Binary file — cannot display content: image.png"
        result = render_file_content(content)
        console = Console(width=80)
        output = console.export_text(result)
        assert "binary" in output.lower() or "cannot display" in output.lower()


class TestRenderSearchResults:
    def test_renders_file_matches(self):
        """Search results show file paths and match types."""
        content = "Found 2 file(s):\n\n[content] src/app.py:42\n  import os\n\n[name]    tests/test_app.py\n  "
        result = render_search_results(content)
        console = Console(width=80)
        output = console.export_text(result)
        assert "app.py" in output


class TestRenderDirectory:
    def test_renders_directory_listing(self):
        """Directory listing shows files and sizes."""
        content = "📁 /home/user/project\n\n  📁 src/\n  📄 main.py  1.2KB\n  📄 README.md  3.4KB\n\n3 items"
        result = render_directory(content)
        console = Console(width=80)
        output = console.export_text(result)
        assert "main.py" in output
        assert "1.2KB" in output


class TestRenderGenericTool:
    def test_renders_simple_message(self):
        """Simple one-line tool results render as inline text."""
        result = render_generic_tool("Stored memory #42: User likes blue")
        console = Console(width=80)
        output = console.export_text(result)
        assert "Stored memory #42" in output

    def test_renders_multiline_in_panel(self):
        """Multi-line tool results render in a panel."""
        content = "Found 3 memories:\n- #1 [note] Hello\n- #2 [fact] World"
        result = render_generic_tool(content)
        console = Console(width=80)
        output = console.export_text(result)
        assert "Hello" in output


class TestGetRenderer:
    def test_known_tool_routes_correctly(self):
        """Known tool names return the right renderer."""
        assert get_renderer("web_search") is render_web_search
        assert get_renderer("read_file") is render_file_content
        assert get_renderer("search_files") is render_search_results
        assert get_renderer("list_directory") is render_directory

    def test_unknown_tool_returns_default(self):
        """Unknown tool names return the default renderer."""
        assert get_renderer("store_memory") is render_generic_tool
        assert get_renderer("random_thing") is render_generic_tool
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_renders.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'ares.renders'`

- [ ] **Step 3: Write the implementation**

Create `ares/renders.py`:

```python
"""Rich renderers for tool results — each tool type gets its own visual treatment."""

from __future__ import annotations

import json
import re
from typing import Any

from rich.console import Group
from rich.markdown import Markdown
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text
from rich.tree import Tree


# ── Web Search Renderer ────────────────────────────────────────

def render_web_search(content: str) -> Any:
    """Render web search results as numbered cards in a Panel."""
    try:
        results = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return Panel(Text(content), title="🔍 Web Search", border_style="bright_green")

    if not results:
        return Panel(
            Text("No results found.", style="dim"),
            title="🔍 Web Search",
            border_style="bright_green",
        )

    renderables = []
    for i, r in enumerate(results, 1):
        title = r.get("title", "Untitled")
        url = r.get("url", "")
        snippet = r.get("snippet", "")

        # Number + title
        title_text = Text()
        title_text.append(f"  {i}  ", style="bold cyan")
        title_text.append(title, style="bold")

        # URL in dim
        url_text = Text(f"     {url}", style="dim underline")

        # Snippet
        snippet_text = Text(f"     {snippet[:120]}", style="dim italic")

        renderables.append(title_text)
        renderables.append(url_text)
        renderables.append(snippet_text)
        if i < len(results):
            renderables.append(Text())  # blank line between results

    return Panel(
        Group(*renderables),
        title="🔍 Web Search Results",
        border_style="bright_green",
        padding=(0, 1),
    )


# ── File Content Renderer ──────────────────────────────────────

def render_file_content(content: str) -> Any:
    """Render file content with syntax highlighting and line numbers."""
    # Detect binary file message
    if "binary file" in content.lower() or "cannot display" in content.lower():
        return Panel(
            Text(content, style="yellow"),
            title="📄 Binary File",
            border_style="yellow",
        )

    # Try to extract filename from header
    title = "📄 File Content"
    header_match = re.search(r"\[File: (.+?) \((\d+) lines", content)
    if header_match:
        filename = header_match.group(1)
        total_lines = header_match.group(2)
        title = f"📄 {filename} ({total_lines} lines)"

    # Extract the code portion (after header line)
    lines = content.split("\n")
    code_lines = []
    header_parts = []

    for line in lines:
        # Lines starting with digits are code with line numbers
        stripped = line.lstrip()
        if stripped and stripped[0].isdigit() and "\t" in line:
            # This is a numbered code line like "     1\tcode here"
            parts = line.split("\t", 1)
            if len(parts) == 2:
                code_lines.append(parts[1])
            else:
                code_lines.append(line)
        elif line.startswith("(") or line.startswith("[File:"):
            header_parts.append(line)
        else:
            if line.strip():
                code_lines.append(line)

    if not code_lines:
        return Panel(Text(content), title=title, border_style="bright_blue")

    code = "\n".join(code_lines)

    # Detect language from filename
    lang = "text"
    if header_match:
        fname = header_match.group(1).lower()
        if fname.endswith(".py"):
            lang = "python"
        elif fname.endswith(".js"):
            lang = "javascript"
        elif fname.endswith(".ts"):
            lang = "typescript"
        elif fname.endswith(".rs"):
            lang = "rust"
        elif fname.endswith(".go"):
            lang = "go"
        elif fname.endswith(".java"):
            lang = "java"
        elif fname.endswith(".c") or fname.endswith(".h"):
            lang = "c"
        elif fname.endswith(".cpp") or fname.endswith(".hpp"):
            lang = "cpp"
        elif fname.endswith(".rb"):
            lang = "ruby"
        elif fname.endswith(".sh") or fname.endswith(".bash"):
            lang = "bash"
        elif fname.endswith(".json"):
            lang = "json"
        elif fname.endswith(".yaml") or fname.endswith(".yml"):
            lang = "yaml"
        elif fname.endswith(".toml"):
            lang = "toml"
        elif fname.endswith(".md"):
            lang = "markdown"
        elif fname.endswith(".html"):
            lang = "html"
        elif fname.endswith(".css"):
            lang = "css"
        elif fname.endswith(".sql"):
            lang = "sql"
        elif fname.endswith(".xml"):
            lang = "xml"

    try:
        syntax = Syntax(
            code,
            lang,
            theme="monokai",
            line_numbers=True,
            word_wrap=True,
        )
        return Panel(syntax, title=title, border_style="bright_blue", padding=(0, 1))
    except Exception:
        # Fallback to plain text
        return Panel(Text(code), title=title, border_style="bright_blue", padding=(0, 1))


# ── File Search Results Renderer ───────────────────────────────

def render_search_results(content: str) -> Any:
    """Render file search results with colored match type badges."""
    if "no results" in content.lower() or "found 0" in content.lower():
        return Panel(
            Text("No matching files found.", style="dim"),
            title="🔎 File Search",
            border_style="bright_yellow",
        )

    table = Table(
        title="🔎 File Search Results",
        border_style="bright_yellow",
        show_header=True,
        header_style="bold",
        title_style="bold bright_yellow",
    )
    table.add_column("Type", width=10)
    table.add_column("Location", ratio=3)
    table.add_column("Excerpt", ratio=5, style="dim italic")

    lines = content.split("\n")
    current_type = ""
    current_path = ""
    current_excerpt = ""

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue

        # Match type badges
        if stripped.startswith("[content]"):
            current_type = "content"
            location = stripped[len("[content]"):].strip()
            current_path = location
            current_excerpt = ""
        elif stripped.startswith("[name]"):
            current_type = "name"
            location = stripped[len("[name]"):].strip()
            current_path = location
            current_excerpt = ""
        elif stripped.startswith("Found") or stripped.startswith("..."):
            continue
        elif current_path and not current_excerpt:
            current_excerpt = stripped[:100]

            # Add row
            type_style = "cyan" if current_type == "content" else "magenta"
            table.add_row(
                Text(current_type, style=f"bold {type_style}"),
                Text(current_path, style="bold"),
                Text(current_excerpt, style="dim italic"),
            )
            current_path = ""
            current_excerpt = ""
            current_type = ""

    if table.row_count == 0:
        # Fallback: just show the content as plain text
        return Panel(Text(content), title="🔎 File Search", border_style="bright_yellow")

    return Panel(table, border_style="bright_yellow", padding=(0, 1))


# ── Directory Listing Renderer ─────────────────────────────────

def render_directory(content: str) -> Any:
    """Render directory listing as a tree with file sizes."""
    lines = content.split("\n")
    title = "📁 Directory"
    tree_items = []
    item_count = ""

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue

        # Extract title from first line
        if stripped.startswith("📁") and title == "📁 Directory":
            title = stripped
            continue

        # Extract item count
        if stripped.endswith("items") or "more items" in stripped:
            item_count = stripped
            continue

        if stripped.startswith("..."):
            item_count = stripped
            continue

        tree_items.append(stripped)

    if not tree_items:
        return Panel(
            Text("Empty directory.", style="dim"),
            title=title,
            border_style="bright_magenta",
        )

    tree = Tree(title, guide_style="dim")
    for item in tree_items:
        if "📁" in item:
            name = item.replace("📁", "").replace("/", "").strip()
            tree.add(Text(f"📁 {name}/", style="bold cyan"))
        elif "📄" in item:
            # Split name and size
            parts = item.replace("📄", "").strip().split()
            if len(parts) >= 2:
                name = parts[0]
                size = " ".join(parts[1:])
                tree.add(Text(f"📄 {name}", style="default"))
            else:
                tree.add(Text(item, style="default"))

    if item_count:
        tree.add(Text(f"\n  {item_count}", style="dim"))

    return Panel(tree, border_style="bright_magenta", padding=(0, 1))


# ── Default Renderer ───────────────────────────────────────────

def render_generic_tool(content: str) -> Any:
    """Render generic tool results — compact panel for multi-line, inline for single-line."""
    lines = content.strip().split("\n")
    if len(lines) <= 2 and len(content) < 120:
        # Simple single-line result — compact inline style
        return Text(f"  ⚙️  {content}", style="dim")

    # Multi-line result — wrap in a panel
    return Panel(
        Text(content),
        title="⚙️ Tool Result",
        border_style="dim",
        padding=(0, 1),
    )


# ── Renderer Registry ──────────────────────────────────────────

RENDERERS: dict[str, Any] = {
    "web_search": render_web_search,
    "read_file": render_file_content,
    "search_files": render_search_results,
    "list_directory": render_directory,
}
DEFAULT_RENDERER = render_generic_tool


def get_renderer(tool_name: str) -> Any:
    """Return the renderer function for a given tool name."""
    return RENDERERS.get(tool_name, DEFAULT_RENDERER)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_renders.py -v`
Expected: All 12 tests PASS

- [ ] **Step 5: Commit**

```bash
git add ares/renders.py tests/test_renders.py
git commit -m "feat: add tool-specific renderers for web, file, search, directory results"
```

---

## Task 2: Update agent token format to include tool name

**Files:**
- Modify: `ares/agent.py:185-186`
- Modify: `tests/test_agent.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_agent.py`:

```python
    def test_process_tool_calls_yields_tool_name(self, agent):
        """Tool results include the tool name in the token format."""
        tool_call = {
            "id": "call_1",
            "function": {
                "name": "store_memory",
                "arguments": json.dumps({"content": "blue", "category": "preference"}),
            },
        }
        results = agent.process_tool_calls([tool_call])
        # The result should contain tool name info for the renderer
        assert "store_memory" in results[0].get("tool_name", "") or "Stored" in results[0]["content"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_agent.py -v`
Expected: FAIL — `tool_name` key doesn't exist in result dict

- [ ] **Step 3: Update `process_tool_calls` to include tool name**

In `ares/agent.py`, update `process_tool_calls` (lines 72-87):

```python
    def process_tool_calls(self, tool_calls: list[dict]) -> list[dict]:
        """Execute tool calls locally and return results with tool names."""
        results = []
        for call in tool_calls:
            try:
                fn = call["function"]
                tool_name = fn["name"]
                args = json.loads(fn.get("arguments") or "{}")
                result = self.tool_executor.execute(tool_name, args)
            except Exception as e:
                tool_name = call.get("function", {}).get("name", "unknown")
                result = f"Error: {e}"
            results.append({
                "tool_call_id": call.get("id", ""),
                "role": "tool",
                "content": result,
                "tool_name": tool_name,
            })
        return results
```

- [ ] **Step 4: Update `run_stream` to yield tool name in token**

In `ares/agent.py`, update the token yield (line 186):

```python
                for tr in tool_results:
                    yield f"[tool:{tr['tool_name']}:{tr['content']}]"
```

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/test_agent.py -v`
Expected: All tests PASS

- [ ] **Step 6: Commit**

```bash
git add ares/agent.py tests/test_agent.py
git commit -m "feat: agent yields tool name in token format for renderer routing"
```

---

## Task 3: Update CLI to route tokens to renderers

**Files:**
- Modify: `ares/cli.py:329-383` (_process_input method)

- [ ] **Step 1: Update _process_input to parse tool name and route**

Replace the `_process_input` method in `ares/cli.py`:

```python
    async def _process_input(self, user_input: str):
        """Process a user message through the agent and display response."""
        from ares.renders import get_renderer, Text

        tool_renderables = []  # collected rich renderables for tool results
        tool_names_used = []   # track which tools were used

        self.console.print()

        with Live(console=self.console, refresh_per_second=10, transient=True) as live:
            live.update(Panel(
                Text(f"{self.icons['thinking']} Thinking...", style="bold italic dim"),
                border_style="dim blue",
            ))

            full_response = ""
            try:
                async for token in self.agent.run_stream(user_input, self.conversation_history):
                    if token.startswith("[tool:"):
                        # Parse: [tool:{tool_name}:{content}]
                        inner = token[6:-1]  # strip [tool: and ]
                        # Split on first colon only — content may contain colons
                        parts = inner.split(":", 1)
                        if len(parts) == 2:
                            tool_name, content = parts
                        else:
                            tool_name = "unknown"
                            content = inner

                        # Show tool status indicator
                        status_icons = {
                            "web_search": ("🔍", "Searching web", "bright_green"),
                            "read_file": ("📄", "Reading file", "bright_blue"),
                            "search_files": ("🔎", "Searching files", "bright_yellow"),
                            "list_directory": ("📁", "Listing directory", "bright_magenta"),
                        }
                        icon, label, color = status_icons.get(
                            tool_name, ("⚙️", "Running tool", "dim")
                        )
                        live.update(Panel(
                            Text(f"{icon} {label}...", style=f"bold {color}"),
                            border_style=color,
                        ))

                        # Render the result using tool-specific renderer
                        renderer = get_renderer(tool_name)
                        renderable = renderer(content)
                        tool_renderables.append(renderable)
                        tool_names_used.append(tool_name)

                    else:
                        full_response += token
                        if full_response.strip():
                            live.update(Markdown(full_response))
            except Exception as e:
                full_response = f"Error: {e}"

        # Show tool results as rich panels
        for renderable in tool_renderables:
            self.console.print(renderable)

        # Show final response
        if full_response.strip():
            self.console.print(Panel(
                Markdown(full_response),
                title=f"{self.icons['bot']} Ares",
                border_style="bright_blue",
                padding=(0, 1),
            ))

        # Update conversation history
        self.conversation_history.append({"role": "user", "content": user_input})
        self.conversation_history.append({"role": "assistant", "content": full_response})
        self.conversation_store.add_exchange(self.conversation_id, user_input, full_response)

        # Trim conversation history
        max_msgs = self.config.max_context_messages
        if len(self.conversation_history) > max_msgs:
            self.conversation_history = self.conversation_history[-max_msgs:]

        self.console.print()
```

- [ ] **Step 2: Run existing tests to verify nothing breaks**

Run: `pytest tests/ -v`
Expected: All existing tests PASS

- [ ] **Step 3: Commit**

```bash
git add ares/cli.py
git commit -m "feat: CLI routes tool results to type-specific Rich renderers"
```

---

## Task 4: Add tool status indicators during streaming

**Files:**
- Modify: `ares/cli.py` (already updated in Task 3)

The tool status indicators are already included in Task 3's `_process_input` update. The status display uses Rich `Live` to show tool-specific messages during execution:

```
  🔍 Searching web...     (during web_search execution)
  📄 Reading file...      (during read_file execution)
  🔎 Searching files...   (during search_files execution)
  📁 Listing directory... (during list_directory execution)
  ⚙️  Running tool...      (for any other tool)
```

These appear as temporary panels inside the `Live` context, then get replaced by the full rendered result after execution completes.

- [ ] **Step 1: Verify status indicators work by running the CLI**

Run: `python -m ares`

Test: type "search the web for python 3.12 features" — should show "🔍 Searching web..." briefly, then render the full search results panel.

- [ ] **Step 2: Commit (if any tweaks needed)**

If the status display needs tweaking after manual testing, commit the fix.

---

## Task 5: Update prompts.py system prompt for new capabilities

**Files:**
- Modify: `ares/prompts.py` (update tool descriptions in SYSTEM_PROMPT)

The system prompt already includes web search and file system instructions from the v2 update. No changes needed unless the new tool descriptions need updating.

- [ ] **Step 1: Verify system prompt already includes all 15 tools**

Run: `python -c "from ares.prompts import SYSTEM_PROMPT; print([t for t in ['web_search', 'read_file', 'search_files', 'list_directory'] if t in SYSTEM_PROMPT])"`

Expected: `['web_search', 'read_file', 'search_files', 'list_directory']`

- [ ] **Step 2: Commit (if any changes needed)**

If the prompt needs updating, commit the changes.

---

## Task 6: Final verification

**Files:**
- All files modified/created

- [ ] **Step 1: Run full test suite**

Run: `pytest tests/ -v --tb=short`
Expected: All tests PASS

- [ ] **Step 2: Visual smoke test**

Run: `python -m ares`

Test these interactions and verify visual quality:
1. "remember that I prefer dark mode" — should show compact tool result (⚙️ inline)
2. "search the web for python 3.12 features" — should show numbered search results panel
3. "list files in my current directory" — should show directory tree
4. "read the file at pyproject.toml" — should show syntax-highlighted code with line numbers
5. "search for files named *.py" — should show search results with match type badges
6. "what do you know about me?" — should recall stored memory

- [ ] **Step 3: Final commit**

```bash
git add -A
git commit -m "feat: CLI UX overhaul — tool-specific Rich renderers and status indicators"
```

---

## Summary

| Task | Files Changed | Tests Added |
|------|---------------|-------------|
| 1. Create renderers module | ares/renders.py | 12 tests |
| 2. Update agent token format | ares/agent.py | 1 test |
| 3. Update CLI routing | ares/cli.py | — |
| 4. Status indicators | (included in Task 3) | — |
| 5. System prompt check | ares/prompts.py | — |
| 6. Final verification | — | Integration |

**Total new tests:** 13
**Total files:** 1 new (renders.py), 2 modified (agent.py, cli.py), 1 new test file
