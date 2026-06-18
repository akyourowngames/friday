# Ares v2 — Streaming Fix, Web Search, File System Access

**Date:** 2026-06-18
**Status:** Draft
**Scope:** Three focused upgrades to the existing Ares assistant

---

## 1. Problem Statement

Ares v1 has three gaps:

1. **Streaming is broken.** The `run_stream` method makes a non-streaming LLM call first to detect tool calls, adding a 5-10 second dead pause before any tokens appear. On free models, this makes the assistant feel frozen.

2. **No web search.** When users ask about current events, weather, news, or anything requiring up-to-date information, Ares either hallucinates or says "I don't know."

3. **No file system access.** Users can't ask Ares to read files, search their codebase, or explore directory structures. This limits Ares to pure conversation without any awareness of the user's local environment.

---

## 2. Design Principles

- **Read-only by default.** Ares can read and search files but cannot write, edit, or execute commands. This eliminates an entire class of safety concerns and matches the user's explicit request.
- **Zero-config where possible.** Web search should work without API keys. File search should work without index setup.
- **Graceful degradation.** If web search fails (rate limits, network), Ares tells the user. If a file doesn't exist, clear error. No silent failures.
- **Token-efficient.** File content and search results are truncated to avoid blowing context windows.

---

## 3. Component: Streaming Fix

### Root Cause

In `agent.py:run_stream`, the current flow is:

```
1. Non-streaming LLM call (waits for full response)
2. If tool_calls → execute tools → go to step 1
3. If no tool_calls → stream the text response
```

Step 1 blocks for 5-10 seconds on free models with zero output. The user sees nothing.

### Fix

**Approach: Streaming-first with mid-stream tool call detection.**

Replace the non-streaming tool detection with streaming detection. Parse SSE delta chunks as they arrive. When a `tool_calls` delta chunk appears (OpenAI streaming format sends tool calls incrementally), stop streaming, execute tools, then start a new streaming call.

**SSE tool call parsing:**

```python
# OpenAI streaming format for tool calls:
# chunk.choices[0].delta.tool_calls[0].function.name
# chunk.choices[0].delta.tool_calls[0].function.arguments
#
# Tool calls arrive in delta chunks:
#   chunk 1: {index: 0, id: "call_123", function: {name: "store_memory"}}
#   chunk 2: {index: 0, function: {arguments: '{"content":'}}
#   chunk 3: {index: 0, function: {arguments: '"dark mode"}'}}
#
# Accumulate arguments across chunks until complete, then execute.
```

**Updated `run_stream` flow:**

```
1. Start streaming LLM call
2. For each delta chunk:
   a. If delta.content → yield the token (user sees it immediately)
   b. If delta.tool_calls → accumulate tool call arguments
3. When stream ends:
   a. If tool_calls were accumulated → execute them, start new stream (go to step 1)
   b. If no tool_calls → we're done (tokens already yielded)
```

**Updated `LLMClient.chat_stream`:**

Change to yield richer chunks — not just text tokens, but also tool call deltas. The `chat_stream` method currently only yields `content` strings. It needs to yield structured chunks:

```python
# Yield format:
{"type": "content", "text": "..."}      # text token
{"type": "tool_call", "index": 0, "id": "call_123", "name": "store_memory"}
{"type": "tool_call_delta", "index": 0, "arguments": "..."}
{"type": "done"}
```

**Updated `Agent.run_stream`:**

Accumulate tool calls from stream deltas, execute them when the stream ends, then restart streaming with tool results appended to messages. Max 5 iterations (same as before).

**Impact:** Tokens appear immediately. Tool calls show as inline yellow panels as they're detected. The 5-10 second dead pause is eliminated.

---

## 4. Component: Web Search

### Research Findings

The `ddgs` package (formerly `duckduckgo-search`) is the clear winner:
- Zero API key required
- Multi-backend support (bing, brave, mojeek, duckduckgo)
- Bing backend is most reliable, DDG backend is rate-limit-prone
- Reuse `DDGS()` instance to avoid rate limits
- `pip install ddgs`

SearXNG is a viable self-hosted fallback but adds infrastructure overhead. Not needed for v1 of web search.

### Tool Definition

```python
{
    "name": "web_search",
    "description": (
        "Search the web for current information. Use when the user asks about "
        "current events, weather, news, recent developments, or any question "
        "that requires up-to-date information you don't have from memory."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "The search query"
            },
            "max_results": {
                "type": "integer",
                "description": "Maximum results to return (default 5)",
                "default": 5
            }
        },
        "required": ["query"]
    }
}
```

### Implementation: `ares/web.py`

```python
"""Web search using ddgs (DuckDuckGo Search) with multi-backend failover."""

import logging
from ddgs import DDGS
from ddgs.exceptions import RatelimitException, TimeoutException

logger = logging.getLogger(__name__)

# Bing first (most reliable), DDG last (rate-limit-prone)
BACKEND_PRIORITY = ["bing", "brave", "mojeek", "duckduckgo"]

def web_search(
    query: str,
    max_results: int = 5,
    max_retries: int = 2,
) -> list[dict[str, str]]:
    """
    Search the web with automatic backend failover.
    Returns list of dicts: {title, url, snippet}
    """
    ddgs = DDGS(timeout=10)

    for attempt in range(max_retries + 1):
        for backend in BACKEND_PRIORITY:
            try:
                results = ddgs.text(query, max_results=max_results, backend=backend)
                return [
                    {"title": r["title"], "url": r["href"], "snippet": r["body"]}
                    for r in results
                ]
            except RatelimitException:
                logger.warning(f"Rate limited on {backend}, trying next")
                continue
            except TimeoutException:
                logger.warning(f"Timeout on {backend}, trying next")
                continue
            except Exception as e:
                logger.warning(f"Error on {backend}: {e}")
                continue

    return []

def format_results(results: list[dict]) -> str:
    """Format search results for LLM consumption."""
    if not results:
        return "No results found."
    lines = [f"Found {len(results)} results:\n"]
    for i, r in enumerate(results, 1):
        lines.append(f"{i}. **{r['title']}**")
        lines.append(f"   {r['url']}")
        lines.append(f"   {r['snippet']}\n")
    return "\n".join(lines)
```

### ToolExecutor Integration

```python
def _web_search(self, args: dict) -> str:
    query = args["query"]
    max_results = args.get("max_results", 5)
    results = web_search(query, max_results=max_results)
    return format_results(results)
```

### System Prompt Update

Add to `SYSTEM_PROMPT`:

```
## Web Search
Use `web_search` when:
- The user asks about current events, news, weather, or recent developments
- The user asks a factual question you're unsure about
- The user asks "what is [something]" and you might not have current info

Do NOT search for:
- Things you already know from memory
- Personal questions about the user
- Tasks/reminders (use tools for those)
```

### Dependency

```toml
dependencies = [
    ...
    "ddgs>=9.0",
]
```

---

## 5. Component: File System Access

### Research Findings

Studied how 6 tools implement file access:

| Tool | Content Search | Name Search | Large Files | Safety |
|------|---------------|-------------|-------------|--------|
| **Claude Code** | ripgrep (subprocess) | glob npm library | 2000-line reads, pagination | Read-before-edit, mtime checks |
| **Codex CLI** | ripgrep (first-class tool) | glob_file_search tool | Truncation policy | 4-layer sandbox |
| **Continue.dev** | ripgrep (grepSearch) | globSearch | readFileRange | 3-tier permissions |
| **Aider** | No separate search | repomap (tree-sitter) | Structural abbrevation | Git-backed |
| **SWE-agent** | bash + grep | bash + find | WindowedFile (fixed window) | 3-layer blocklist |
| **Cursor** | Vector embeddings | Tree-sitter AST | Semantic chunking | Privacy mode |

**Universal pattern:** Content search via ripgrep, name search via glob, read-only by default.

**Key insight:** Every tool uses ripgrep for content search. It's the universal standard — fast, respects .gitignore, regex support. But Ares is a personal assistant, not a coding tool. We need a hybrid approach that works for both code and general files.

### Design: Three Tools

#### Tool 1: `read_file`

```python
{
    "name": "read_file",
    "description": (
        "Read the contents of a file. Returns the file content with line numbers. "
        "Use when the user asks about a specific file, wants to see file contents, "
        "or references a file path."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Absolute or relative path to the file"
            },
            "start_line": {
                "type": "integer",
                "description": "Line number to start reading from (1-based, default 1)",
                "default": 1
            },
            "num_lines": {
                "type": "integer",
                "description": "Number of lines to read (default 200, max 2000)",
                "default": 200
            }
        },
        "required": ["path"]
    }
}
```

**Implementation:** `ares/filesystem.py`

```python
async def read_file(path: str, start_line: int = 1, num_lines: int = 200) -> str:
    """Read a file with line numbers. Returns formatted content."""
    # Validate path exists
    # Detect binary files (check for null bytes in first 1KB)
    # Read lines, apply start_line/num_lines
    # Format with line numbers (cat -n style)
    # Add context indicators:
    #   "[File: path (N lines total)]"
    #   "(X more lines above)" if start_line > 1
    #   "(Y more lines below)" if truncated
    # Max 2000 lines per read
```

**Safety:** Path validation (no traversal outside home directory), binary detection, size limits.

#### Tool 2: `search_files`

```python
{
    "name": "search_files",
    "description": (
        "Search for files by name pattern AND/OR content. Finds files even when "
        "you don't know exact names or locations. Use when the user asks to find "
        "files, search for something in their codebase, or locate a file."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Content to search for (regex supported)"
            },
            "path": {
                "type": "string",
                "description": "Directory to search in (default: home directory)"
            },
            "name_pattern": {
                "type": "string",
                "description": "Glob pattern to filter by file name (e.g. '*.py', '*.md')"
            },
            "max_results": {
                "type": "integer",
                "description": "Max results to return (default 20)",
                "default": 20
            }
        },
        "required": ["query"]
    }
}
```

**Implementation:** Hybrid search combining two strategies:

```python
async def search_files(
    query: str = "",
    path: str = "~",
    name_pattern: str = "",
    max_results: int = 20,
) -> str:
    """
    Hybrid file search: content + name.

    Strategy:
    1. Content search: Use ripgrep (if available) or Python re module
    2. Name search: Use pathlib glob patterns
    3. Merge and deduplicate
    4. Sort by relevance (content matches first, then name matches)
    """
    results = {}

    # Content search (ripgrep preferred)
    if query:
        content_matches = await _content_search(query, path, name_pattern)
        for match in content_matches:
            results[match["path"]] = match

    # Name search (always if pattern provided)
    if name_pattern:
        name_matches = await _name_search(name_pattern, path)
        for match in name_matches:
            if match["path"] not in results:
                results[match["path"]] = match

    # Format output
    return _format_search_results(list(results.values())[:max_results])
```

**Content search implementation:**

```python
async def _content_search(query: str, path: str, name_pattern: str) -> list[dict]:
    """Search file contents using ripgrep or fallback to Python re."""
    # Try ripgrep first (fast, respects .gitignore)
    try:
        cmd = ["rg", "-n", "--max-columns", "500", "--max-count", "3"]
        if name_pattern:
            cmd.extend(["-g", name_pattern])
        cmd.extend([query, expanded_path])
        result = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, _ = await result.communicate()
        return _parse_ripgrep_output(stdout.decode())
    except FileNotFoundError:
        pass

    # Fallback: Python re module
    return await _python_content_search(query, path, name_pattern)
```

**Name search implementation:**

```python
async def _name_search(pattern: str, path: str) -> list[dict]:
    """Search files by name using pathlib glob."""
    expanded = Path(path).expanduser()
    matches = []
    for p in expanded.rglob(pattern):
        if p.is_file():
            matches.append({
                "path": str(p),
                "match_type": "name",
                "excerpt": "",
                "line": 0,
            })
    return matches
```

**Output format:**

```
Found 8 files:

[content match] src/memory.py:45
  "SELECT * FROM facts_meta WHERE fact_id = ?"

[content match] src/memory.py:112
  "results = self.memory.search(query, limit=limit)"

[name match] src/tests/test_memory.py
  (test file for memory system)

... 5 more results
```

#### Tool 3: `list_directory`

```python
{
    "name": "list_directory",
    "description": (
        "List the contents of a directory. Shows files and subdirectories "
        "with sizes. Use when the user wants to explore a directory or "
        "see what files exist."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Directory path (default: current directory)"
            },
            "max_items": {
                "type": "integer",
                "description": "Max items to show (default 30)",
                "default": 30
            }
        }
    }
}
```

**Implementation:**

```python
async def list_directory(path: str = ".", max_items: int = 30) -> str:
    """List directory contents with file sizes."""
    expanded = Path(path).expanduser().resolve()
    items = sorted(expanded.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))

    lines = [f"📁 {expanded}\n"]
    shown = 0
    for item in items:
        if shown >= max_items:
            remaining = len(items) - shown
            lines.append(f"\n... and {remaining} more items")
            break
        if item.is_dir():
            lines.append(f"  📁 {item.name}/")
        else:
            size = item.stat().st_size
            lines.append(f"  📄 {item.name}  {_format_size(size)}")
        shown += 1

    return "\n".join(lines)
```

### Safety Model

| Rule | Implementation |
|------|---------------|
| **Read-only** | No write/edit/shell tools — only read_file, search_files, list_directory |
| **Home directory limit** | Path validation: reject paths outside `~` (configurable) |
| **Binary detection** | Check first 1KB for null bytes → return "Binary file, cannot display" |
| **Size limits** | Max 2000 lines per read, max 50KB per search result set |
| **No symlink traversal** | Resolve symlinks before path validation |
| **.gitignore respected** | ripgrep respects it by default; Python fallback skips .git, __pycache__, node_modules |

### Path Resolution

```python
def resolve_path(path: str) -> Path:
    """Resolve and validate a file path."""
    expanded = Path(path).expanduser().resolve()

    # Block traversal outside home
    home = Path.home()
    if not str(expanded).startswith(str(home)):
        raise ValueError(f"Access denied: {path} is outside home directory")

    return expanded
```

---

## 6. System Prompt Updates

Add to existing `SYSTEM_PROMPT`:

```
## File System Access

You can read files and search the user's file system.

- Use `read_file` when the user references a specific file or wants to see file contents
- Use `search_files` when the user wants to find files by name or content
- Use `list_directory` when the user wants to explore a directory

Rules:
- Always show file paths relative to the user's home directory when possible
- When reading large files, read only the relevant section
- When searching, start broad and narrow down
- Never modify files — you can only read

## Web Search

Use `web_search` when:
- The user asks about current events, news, weather, or recent developments
- The user asks a factual question you're unsure about
- The user asks "what is [something]" and you might not have current info

Do NOT search for:
- Things you already know from memory
- Personal questions about the user
- Tasks/reminders (use tools for that)
```

---

## 7. Updated Tool List

| # | Tool | Type | Permission |
|---|------|------|------------|
| 1 | `store_memory` | Memory | Auto |
| 2 | `search_memory` | Memory | Auto |
| 3 | `update_memory` | Memory | Auto |
| 4 | `delete_memory` | Memory | Auto |
| 5 | `create_task` | Tasks | Auto |
| 6 | `list_tasks` | Tasks | Auto |
| 7 | `search_tasks` | Tasks | Auto |
| 8 | `complete_task` | Tasks | Auto |
| 9 | `cancel_task` | Tasks | Auto |
| 10 | `get_due_soon` | Tasks | Auto |
| 11 | `export_data` | Data | Auto |
| 12 | `web_search` | **NEW** Web | Auto |
| 13 | `read_file` | **NEW** Files | Auto |
| 14 | `search_files` | **NEW** Files | Auto |
| 15 | `list_directory` | **NEW** Files | Auto |

Total: 15 tools (11 existing + 4 new).

---

## 8. New Dependencies

```toml
dependencies = [
    "rich>=13.0",
    "prompt_toolkit>=3.0",
    "sentence-transformers[onnx]>=3.2",
    "sqlite-vec>=0.1",
    "httpx>=0.27",
    "pydantic>=2.0",
    "dateparser>=1.2",
    "tzlocal>=5.0",
    "plyer>=2.1",
    "ddgs>=9.0",                    # NEW: web search
]
```

No additional dependencies for file system access — uses stdlib (`pathlib`, `asyncio.subprocess`, `os`).

---

## 9. Error Handling

| Scenario | Handling |
|----------|----------|
| Web search rate limited | Failover to next backend, then report to user |
| Web search network error | "Web search unavailable — check your connection" |
| File not found | "File not found: {path}" |
| Permission denied | "Permission denied: {path}" |
| Binary file | "Binary file — cannot display content" |
| File too large | "File too large ({N} lines). Use start_line/num_lines to read a section." |
| Search outside home | "Access restricted to home directory" |
| ripgrep not installed | Fall back to Python re module automatically |

---

## 10. Testing Strategy

- **Unit tests:** Each new module (web.py, filesystem.py) gets its own test file
- **Web search tests:** Mock `DDGS.text()` to avoid network calls in tests
- **File system tests:** Use `tmp_path` fixtures, create test files, verify read/search/list
- **Streaming tests:** Mock LLM responses with tool call deltas to verify accumulation logic
- **Integration test:** Full agent loop with mocked LLM that calls web_search and read_file

---

## 11. Out of Scope (Explicitly Not Building)

- File write/edit capabilities
- Shell command execution
- Write permissions or approval flows
- Semantic code search (embeddings for code)
- Tree-sitter AST parsing
- Self-hosted search backends (SearXNG)
- Voice input/output

---

## 12. Success Criteria

- [ ] Streaming works — tokens appear immediately, no dead pause
- [ ] Tool calls detected mid-stream without blocking
- [ ] Web search returns results without API key
- [ ] Web search fails gracefully on rate limits
- [ ] File reading shows line numbers and truncation indicators
- [ ] Content search finds files by text pattern
- [ ] Name search finds files by glob pattern
- [ ] Directory listing shows sizes and structure
- [ ] Binary files detected and handled
- [ ] Path validation prevents outside-home access
- [ ] All existing tests still pass
- [ ] New tests cover all new functionality
