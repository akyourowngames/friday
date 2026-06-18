# Ares CLI UX Overhaul — Design Spec

**Date:** 2026-06-18
**Status:** Approved
**Scope:** CLI rendering pipeline rebuild — tool results, streaming display, prompt feel

---

## 1. Problem Statement

The v2 features (web search, file system, streaming) shipped, but the CLI display system wasn't updated to handle them. Current issues:

- **Web search results** crammed into tiny yellow `[tool:...]` notification panels — titles, URLs, and snippets all run together, unreadable
- **File content** rendered as raw text in a yellow box — no syntax highlighting, no line numbers, no visual structure
- **Directory listings** same yellow box treatment — no tree view, no file size formatting
- **Search results** (file search) same problem — content matches and name matches not visually distinguished
- **Tool result panels** all look identical — can't tell at a glance what tool was used
- **Streaming display** works but the transition between "thinking" → tool results → response text feels jarring

The assistant works, but it looks like a prototype, not a polished tool.

---

## 2. Design Principles

- **Tool-specific rendering.** Each tool type gets its own Rich renderable. Web search looks like search results. Files look like code. Directories look like file trees.
- **Instant recognition.** You should be able to glance at the terminal and immediately know what just happened — "searched web", "read a file", "listed a directory".
- **Minimal visual noise.** No unnecessary borders, padding, or decoration. Content first.
- **Graceful fallback.** Unknown tool types get a clean default renderer, not a crash.

---

## 3. Architecture: Rendering Pipeline

### Current Flow (broken)

```
Agent yields token: "[tool:{content}]"
  → CLI strips [tool:...] tags
  → Shows content in yellow Panel()
  → All tools look the same
```

### New Flow

```
Agent yields token: "[tool:{tool_name}:{content}]"
  → CLI extracts tool_name and content
  → Routes to matching renderer in renders.py
  → Renderer returns a Rich renderable (Panel, Table, Syntax, etc.)
  → Renderable is displayed with styled header
```

### Token Format Change

In `agent.py`, `process_tool_calls` currently yields `[tool:{content}]`. Change to:

```python
yield f"[tool:{tool_name}:{content}]"
```

Where `tool_name` is extracted from the tool call's function name. The CLI parses this to route to the right renderer.

### Renderer Registry

```python
# ares/renders.py

RENDERERS: dict[str, Callable] = {
    "web_search": render_web_search,
    "read_file": render_file_content,
    "search_files": render_search_results,
    "list_directory": render_directory,
}
DEFAULT_RENDERER = render_generic_tool

def get_renderer(tool_name: str) -> Callable:
    return RENDERERS.get(tool_name, DEFAULT_RENDERER)
```

---

## 4. Renderer: Web Search

**Input:** JSON string from `web_search` tool — list of `{title, url, snippet}` dicts.

**Output:** Rich Panel with numbered result cards.

```
╭─ 🔍 Web Search: "python 3.12 features" ─────╮
│                                               │
│  1  Python 3.12 Features                      │
│     docs.python.org/3.12                      │
│     New typing features and performance...    │
│                                               │
│  2  What's New in Python 3.12                 │
│     realpython.com/python-312-new-features    │
│     Explore the exciting new features...      │
│                                               │
│  3  PEP 695 – Type Parameter Syntax           │
│     peps.python.org/pep-0695                  │
│     A new way to define type variables...     │
╰───────────────────────────────────────────────╯
```

**Rendering details:**
- Title: bold, cyan
- URL: dim, underlined
- Snippet: normal text, truncated at 80 chars
- Panel title includes the search query in dim
- Panel border: bright_green

---

## 5. Renderer: File Content

**Input:** String from `read_file` tool — content with line numbers and headers.

**Output:** Rich Syntax block (for syntax highlighting) inside a Panel.

```
╭─ 📄 ares/llm.py (98 lines) ──────────────────╮
│  1  │ """LLM API client. """                   │
│  2  │                                          │
│  3  │ import json                              │
│  4  │ from typing import AsyncIterator         │
│  5  │                                          │
│  6  │ import httpx                             │
│ ...  │                                         │
│  50  │     resp = await self._client.post(     │
╰───────────────────────────────────────────────╯
```

**Rendering details:**
- Use Rich's `Syntax` renderer with language detection (from file extension)
- Line numbers in dim style, right-aligned
- Panel title shows filename and total line count
- Panel border: bright_blue
- If file has >50 lines, show first 20 + last 10 with "... N more lines" separator
- Binary files: show a warning panel instead (yellow border)

**Fallback:** If the content string can't be parsed into structured lines, render as plain text in a Panel.

---

## 6. Renderer: File Search Results

**Input:** String from `search_files` tool — formatted search results with match types.

**Output:** Rich Table with colored match type badges.

```
╭─ 🔎 File Search ─────────────────────────────╮
│                                               │
│  [content] ares/llm.py:45                     │
│    "SELECT * FROM facts_meta WHERE fact_id"   │
│                                               │
│  [content] ares/llm.py:112                    │
│    "results = self.memory.search(query, ..."   │
│                                               │
│  [name]    src/tests/test_memory.py           │
│    (test file for memory system)              │
│                                               │
│  Found 8 files (showing top 8)                │
╰───────────────────────────────────────────────╯
```

**Rendering details:**
- Match type as colored badge: `[content]` in cyan, `[name]` in magenta
- File path in bold
- Line number in dim after path (if content match)
- Excerpt below path in dim italic
- Panel border: bright_yellow

---

## 7. Renderer: Directory Listing

**Input:** String from `list_directory` tool — formatted directory contents.

**Output:** Rich Tree or Table with file sizes.

```
╭─ 📁 ~/projects/ares ─────────────────────────╮
│                                               │
│  📁 ares/                                     │
│  📁 tests/                                    │
│  📁 docs/                                     │
│  📄 pyproject.toml           1.2KB            │
│  📄 README.md               3.4KB            │
│  📄 .gitignore               128B            │
│                                               │
│  6 items                                      │
╰───────────────────────────────────────────────╯
```

**Rendering details:**
- Directories listed first (sorted alphabetically), then files
- File sizes formatted: B, KB, MB
- Directories have 📁 prefix, files have 📄 prefix
- Panel title shows the directory path (tilde-expanded)
- Panel border: bright_magenta
- If "... and N more items" is in the content, show it in dim at the bottom

---

## 8. Default Renderer (fallback)

For tools that don't have a specific renderer (store_memory, search_memory, create_task, etc.), show the result in a compact panel:

```
  ⚙️ Stored memory #42: User prefers dark mode
```

or for multi-line results:

```
╭─ ⚙️ Task #5 ─────────────────────────────────╮
│ Created task: Call dentist (due: 2026-06-19)  │
╰───────────────────────────────────────────────╯
```

**Rendering details:**
- Single-line results: inline with ⚙️ icon, no panel border
- Multi-line results: thin Panel with dim border
- Panel border: dim

---

## 9. Streaming Display Improvements

### Current Problems
- "Thinking..." indicator disappears the moment the first token arrives
- Tool results flash and disappear (transient=True)
- No visual distinction between "streaming response" and "tool executing"

### New Behavior

1. **Thinking phase:** Show "🤔 Thinking..." with a spinner (Rich Live handles this)
2. **Tool execution phase:** Replace thinking indicator with tool-specific header
   - "🔍 Searching web for..." while web_search executes
   - "📄 Reading file..." while read_file executes
   - "🔎 Searching files..." while search_files executes
   - "📁 Listing directory..." while list_directory executes
3. **Response streaming:** Show response text as it streams in (existing behavior, kept)
4. **Tool result display:** After streaming completes, show tool results as rendered panels ABOVE the final response

### Tool Status Indicators

Add a `_tool_status` method to `AresCLI`:

```python
TOOL_STATUS = {
    "web_search": ("🔍", "Searching web for", "bright_green"),
    "read_file": ("📄", "Reading", "bright_blue"),
    "search_files": ("🔎", "Searching files for", "bright_yellow"),
    "list_directory": ("📁", "Listing", "bright_magenta"),
}
```

When a tool starts executing, show a brief status line (not a panel) in the streaming area. This gives the user feedback that something is happening.

---

## 10. Prompt and Banner Refresh

### Banner
Current banner is functional but plain. Update to:
- Model name in cyan
- Memory count in green
- Task count in yellow
- Version number in dim

### Prompt
Current prompt is `❯ `. Keep it — it's clean and minimal. No changes.

### Exit message
Current "Goodbye! 👋" is fine. No changes.

---

## 11. Files to Create/Modify

| File | Action | What Changes |
|------|--------|--------------|
| `ares/renders.py` | **Create** | All renderers (web, file, search, directory, default) |
| `ares/cli.py` | Modify | Route tool tokens to renderers, update _process_input, add tool status indicators |
| `ares/agent.py` | Modify | Yield tool name in token format `[tool:{name}:{content}]` |
| `tests/test_renders.py` | **Create** | Unit tests for each renderer |

**Total files:** 1 new, 3 modified, 1 new test file.

---

## 12. Error Handling

| Scenario | Handling |
|----------|----------|
| Tool result not valid JSON (web search) | Fall back to default renderer, show raw text |
| File content too wide for terminal | Rich handles wrapping automatically |
| Unknown tool name | Use default renderer (compact panel) |
| Empty tool result | Show "[dim]No results[/dim]" |
| Renderer raises exception | Catch and fall back to default renderer |

---

## 13. Testing Strategy

- **Unit tests:** Each renderer tested with mock data
- **Integration:** CLI _process_input tested with mocked agent yielding tool tokens
- **Visual:** Manual smoke test — run `python -m ares` and trigger each tool type

---

## 14. Out of Scope

- New slash commands
- Interactive confirmations
- Keyboard shortcuts
- Tab completion for file paths
- History search
- New backend features
- Changes to the agent/LLM layer beyond token format

---

## 15. Success Criteria

- [ ] Web search results render as numbered cards with title/URL/snippet separation
- [ ] File content renders with syntax highlighting and line numbers
- [ ] File search results show match type badges (content vs name)
- [ ] Directory listings show file tree with sizes
- [ ] Tool execution shows status indicators during streaming
- [ ] All existing tests still pass
- [ ] New renderer tests cover all tool types
- [ ] Visual quality matches Codex/Claude Code standard
