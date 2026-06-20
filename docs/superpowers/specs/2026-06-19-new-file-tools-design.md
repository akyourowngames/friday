# New File Tools for Ares — Design Spec

**Date:** 2026-06-19
**Status:** Implemented
**Author:** Claude (brainstorming session)

---

## Overview

Expand Ares's file capabilities from 3 read-only tools to a full suite of 8 tools covering both reading and writing. Write operations are sandboxed to the home directory with tiered safety mechanisms.

## Current State

Ares has 3 file tools in `ares/filesystem.py` (read-only):
- `read_file` — read a file with line numbers
- `search_files` — search by content regex and/or name glob
- `list_directory` — list directory contents

All are sandboxed to `~` via `resolve_path()`.

## New Tools

### Read Tools (no sandbox — can read any path)

#### `glob_pattern`
Find files matching a glob pattern.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `pattern` | string | required | Glob pattern (e.g. `**/*.py`, `src/**/*.ts`) |
| `path` | string | `"."` | Directory to search from |
| `max_results` | integer | `50` | Max files to return |

- Uses `pathlib.Path.glob()` / `rglob()`
- Skips `SKIP_DIRS` (`.git`, `node_modules`, `__pycache__`, etc.)
- Returns sorted list of matching file paths with sizes
- No sandbox restriction

#### `get_file_info`
Get metadata about a file or directory.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `path` | string | required | File or directory path |

Returns: type (file/dir/symlink), size (human-readable), created/modified/accessed timestamps, permissions, whether it's a binary file (for files).

No sandbox restriction.

### Write Tools (sandboxed to `~`)

#### `write_file`
Create a new file or overwrite an existing one.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `path` | string | required | File path to write |
| `content` | string | required | File content |
| `dry_run` | bool | `false` | Preview without writing |
| `confirm` | bool | `false` | Confirm destructive overwrite |

**Behavior:**
- If file doesn't exist: creates it (and parent directories). No confirmation needed.
- If file exists and `confirm=false`: returns confirmation prompt with file size.
- If file exists and `confirm=true`: overwrites.
- If `dry_run=true`: returns what would happen without touching anything.

#### `edit_file`
Search and replace within an existing file.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `path` | string | required | File path |
| `old_text` | string | required | Text to find (must match uniquely) |
| `new_text` | string | required | Replacement text |
| `dry_run` | bool | `false` | Preview without applying |

**Matching cascade** (adapted from aider):
1. Exact line-by-line match
2. Whitespace-normalized match (handles LLM indentation mistakes)
3. No match → error with "did you mean?" suggestion (closest content, threshold 0.6)
4. Multiple matches → error asking for more context

This tool is non-destructive (it modifies, doesn't delete), so no confirmation prompt.

#### `create_directory`
Create a directory with parents (`mkdir -p` behavior).

| Parameter | Type | Default | Description |
|---|---|---|---|
| `path` | string | required | Directory path |
| `dry_run` | bool | `false` | Preview without creating |

No confirmation needed (non-destructive).

#### `delete_file`
Delete a file or empty directory.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `path` | string | required | File or directory path |
| `confirm` | bool | `false` | Confirm deletion |
| `dry_run` | bool | `false` | Preview without deleting |

- Refuses non-empty directories (error with contents listed)
- Always requires `confirm=true` (destructive, irreversible)

#### `move_file`
Move or rename a file/directory.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `source` | string | required | Current path |
| `destination` | string | required | New path |
| `confirm` | bool | `false` | Confirm if destination exists |
| `dry_run` | bool | `false` | Preview without moving |

- Creates parent directories of destination as needed
- If destination exists and `confirm=false`: returns confirmation prompt with destination size
- If destination exists and `confirm=true`: overwrites destination

---

## Safety Model

### Tiered Approval

| Operation | Risk | Approval |
|---|---|---|
| `glob_pattern`, `get_file_info`, `read_file`, `search_files`, `list_directory` | Read-only | None |
| `write_file` (new file) | Low | None |
| `create_directory` | Low | None |
| `edit_file` | Medium | None (non-destructive, reversible via read) |
| `write_file` (overwrite existing) | High | Requires `confirm=true` |
| `delete_file` | High | Requires `confirm=true` |
| `move_file` (to existing destination) | High | Requires `confirm=true` |

### Sandbox Rules

- **Reads:** No restriction — can read any path the OS user can access
- **Writes:** Confined to `~` (home directory)
- **Protected paths:** `~/.ares/config.json` and `~/.ares/data/` are write-blocked entirely (Ares cannot modify its own config/data)

### Dry-Run

Every write tool supports `dry_run: bool = false`. When true, returns a human-readable description of what would happen without any filesystem changes. Always safe to call.

---

## File Structure

```
ares/
├── filesystem.py            # Existing reads + new read tools (glob_pattern, get_file_info)
├── filesystem_write.py      # NEW — all write operations with sandboxing
├── tools.py                 # Updated — new tool definitions + handlers
```

### `filesystem_write.py` imports from `filesystem.py`:
- `_allowed_roots()` — for write sandbox validation
- `_display_path()` — for human-readable output
- `_format_size()` — for file size display
- `_is_binary()` — for binary file detection

### `resolve_path()` changes in `filesystem.py`:
- Remove the home-directory restriction (allow any path for reads)
- Reads become fully unrestricted

### New `resolve_write_path()` in `filesystem_write.py`:
- Enforces home-directory sandbox
- Blocks writes to `~/.ares/config.json` and `~/.ares/data/`
- Uses same `_allowed_roots()` check

---

## Tool Definitions in `tools.py`

All 8 file tools are registered in `get_tool_definitions()` and wired into `ToolExecutor.execute()`.

### `edit_file` matching cascade implementation

```python
def edit_file(path, old_text, new_text, dry_run=False):
    # 1. Validate path (write sandbox)
    # 2. Read file content
    # 3. Try exact match: content.count(old_text) == 1
    # 4. If no exact match, try whitespace-normalized:
    #    - Strip leading/trailing whitespace from each line in old_text
    #    - Find matching region with same relative indentation
    # 5. If still no match:
    #    - Find closest content using SequenceMatcher (threshold 0.6)
    #    - Return: "No match found. Did you mean:\n{closest}"
    # 6. If multiple matches: return error asking for more context
    # 7. Apply replacement
    # 8. If dry_run: return preview, don't write
    # 9. Write file, return success with line count
```

---

## Error Handling

All tools return structured error messages, not exceptions:

| Scenario | Response |
|---|---|
| File not found | `File not found: {path}` |
| Permission denied | `Permission denied: {path}` |
| Path outside sandbox | `Write denied: {path} is outside home directory` |
| Protected path | `Write denied: {path} is a protected Ares system path` |
| edit_file: no match | `No match found for old_text. Did you mean:\n{closest_content}` |
| edit_file: multiple matches | `old_text matches {N} locations. Provide more context to make it unique.` |
| delete_file: non-empty dir | `Cannot delete: {path} is a non-empty directory ({N} items)` |
| delete_file: no confirm | `⚠ CONFIRM REQUIRED: This will delete {path}. Re-call with confirm=true to proceed.` |
| write_file: overwrite, no confirm | `⚠ CONFIRM REQUIRED: This will overwrite {path} ({size}). Re-call with confirm=true to proceed.` |

---

## Testing Plan

1. **Unit tests for each new tool** in `tests/test_filesystem.py`
2. **Sandbox tests:** verify writes outside `~` are rejected
3. **Protected path tests:** verify `~/.ares/` writes are blocked
4. **Edit cascade tests:** exact match, whitespace match, no match (did-you-mean), multiple matches
5. **Dry-run tests:** verify no filesystem changes occur
6. **Confirmation tests:** verify destructive ops require `confirm=true`
7. **Integration tests:** tool definitions parse correctly, handlers are wired

---

## Out of Scope

- File watching / real-time monitoring
- Undo/redo system (future consideration)
- File permissions/chmod operations
- Symlink creation
- Binary file editing
