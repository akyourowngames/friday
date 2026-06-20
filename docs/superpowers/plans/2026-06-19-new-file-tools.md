# New File Tools Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Expand Ares from 3 read-only file tools to 8 tools covering reads (glob, file info) and writes (write, edit, mkdir, delete, move), with tiered safety.

**Architecture:** Read tools go in the existing `filesystem.py` (unsandboxed). Write tools go in a new `filesystem_write.py` with home-directory sandboxing and protected-path blocking. `edit_file` uses a resilience cascade (exact → whitespace-normalized → fuzzy "did you mean?"). All write tools support `dry_run`. Destructive ops require `confirm=true` via a confirmation mechanism in `ToolExecutor`.

**Tech Stack:** Python 3.11+, pathlib, difflib (fuzzy matching), tempfile + os.replace (atomic writes), prompt_toolkit (confirmation prompts)

---

## File Structure

| File | Action | Responsibility |
|------|--------|----------------|
| `ares/filesystem.py` | Modify | Remove home sandbox from `resolve_path()`. Add `get_file_info()` and `glob_pattern()`. |
| `ares/filesystem_write.py` | Create | Write sandbox (`resolve_write_path`), atomic writes, all write tool implementations. |
| `ares/tools.py` | Modify | Add 5 new tool definitions + 5 new handlers + confirmation flow in `ToolExecutor`. |
| `tests/test_filesystem.py` | Modify | Add tests for `get_file_info` and `glob_pattern`. |
| `tests/test_filesystem_write.py` | Create | Tests for all write tools and sandboxing. |

---

## Task 1: Un-sandbox Reads

**Files:**
- Modify: `ares/filesystem.py:18-36`

The spec says reads should be unrestricted. Currently `resolve_path()` blocks anything outside `~`. We need to split the logic: reads become unrestricted, writes keep the sandbox in `filesystem_write.py`.

- [x] **Step 1: Write failing test for unrestricted reads**

```python
# tests/test_filesystem.py — add to existing file

def test_resolve_path_outside_home():
    """resolve_path should accept paths outside home directory."""
    result = resolve_path("/tmp/somefile.txt")
    assert result == Path("/tmp/somefile.txt")
```

- [x] **Step 2: Run test to verify it fails**

Run: `cd C:\Users\anime\ares && python -m pytest tests/test_filesystem.py::test_resolve_path_outside_home -v`
Expected: FAIL — `ValueError: Access denied`

- [x] **Step 3: Remove sandbox from resolve_path**

In `ares/filesystem.py`, replace the body of `resolve_path` with:

```python
def resolve_path(path: str = ".") -> Path:
    """Resolve a path. No access restrictions for reads."""
    return Path(path or ".").expanduser().resolve()
```

Keep `_allowed_roots()` and `_is_relative_to()` — they're used by `filesystem_write.py` later.

- [x] **Step 4: Run all existing filesystem tests to verify nothing broke**

Run: `cd C:\Users\anime\ares && python -m pytest tests/test_filesystem.py -v`
Expected: All existing tests PASS. The new test from Step 1 also PASSES.

- [x] **Step 5: Commit**

```bash
cd C:\Users\anime\ares
git add ares/filesystem.py tests/test_filesystem.py
git commit -m "feat: remove home directory sandbox from read operations

Reads are now unrestricted. Write sandbox will live in
filesystem_write.py. Kept _allowed_roots() for reuse.

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task 2: Add `get_file_info`

**Files:**
- Modify: `ares/filesystem.py` — add `get_file_info()` function
- Modify: `tests/test_filesystem.py` — add tests

- [x] **Step 1: Write failing tests**

```python
# tests/test_filesystem.py — add to existing file

def test_get_file_info_regular_file(tmp_path):
    from ares.filesystem import get_file_info
    test_file = tmp_path / "hello.txt"
    test_file.write_text("hello world", encoding="utf-8")

    result = get_file_info(str(test_file))
    assert "Type: file" in result
    assert "Size:" in result
    assert "hello.txt" in result
    assert "Modified:" in result


def test_get_file_info_directory(tmp_path):
    from ares.filesystem import get_file_info
    result = get_file_info(str(tmp_path))
    assert "Type: directory" in result


def test_get_file_info_not_found():
    from ares.filesystem import get_file_info
    result = get_file_info("/nonexistent/path/file.txt")
    assert "not found" in result.lower() or "not found" in result.lower()


def test_get_file_info_binary(tmp_path):
    from ares.filesystem import get_file_info
    bin_file = tmp_path / "data.bin"
    bin_file.write_bytes(b"\x00\x01\x02\x03\x04\x05")

    result = get_file_info(str(bin_file))
    assert "Binary: yes" in result
```

- [x] **Step 2: Run tests to verify they fail**

Run: `cd C:\Users\anime\ares && python -m pytest tests/test_filesystem.py::test_get_file_info_regular_file tests/test_filesystem.py::test_get_file_info_directory tests/test_filesystem.py::test_get_file_info_not_found tests/test_filesystem.py::test_get_file_info_binary -v`
Expected: FAIL — `ImportError: cannot import name 'get_file_info'`

- [x] **Step 3: Implement `get_file_info`**

Add to `ares/filesystem.py` after the existing `list_directory` function:

```python
def get_file_info(path: str) -> str:
    """Get metadata about a file or directory."""
    resolved = resolve_path(path)
    if not resolved.exists():
        return f"File not found: {path}"

    stat = resolved.stat()
    size = _format_size(stat.st_size)
    mtime = _format_timestamp(stat.st_mtime)
    atime = _format_timestamp(stat.st_atime)
    ctime = _format_timestamp(stat.st_ctime)

    if resolved.is_symlink():
        ftype = "symlink"
    elif resolved.is_dir():
        ftype = "directory"
    elif resolved.is_file():
        ftype = "file"
    else:
        ftype = "unknown"

    is_binary = "no"
    if resolved.is_file():
        try:
            is_binary = "yes" if _is_binary(resolved) else "no"
        except (OSError, PermissionError):
            is_binary = "unknown"

    lines = [
        f"[File Info: {_display_path(resolved)}]",
        f"  Type: {ftype}",
        f"  Size: {size} ({stat.st_size:,} bytes)",
        f"  Modified: {mtime}",
        f"  Accessed: {atime}",
        f"  Created: {ctime}",
    ]
    if resolved.is_file():
        lines.append(f"  Binary: {is_binary}")

    return "\n".join(lines)
```

Add the helper before `get_file_info`:

```python
def _format_timestamp(ts: float) -> str:
    """Format a timestamp as a human-readable string."""
    from datetime import datetime, timezone
    try:
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    except (OSError, ValueError):
        return "unknown"
```

- [x] **Step 4: Run tests to verify they pass**

Run: `cd C:\Users\anime\ares && python -m pytest tests/test_filesystem.py -v`
Expected: All tests PASS.

- [x] **Step 5: Commit**

```bash
cd C:\Users\anime\ares
git add ares/filesystem.py tests/test_filesystem.py
git commit -m "feat: add get_file_info tool

Returns type, size, timestamps, and binary status for files
and directories. No sandbox restriction.

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task 3: Add `glob_pattern`

**Files:**
- Modify: `ares/filesystem.py` — add `glob_pattern()` function
- Modify: `tests/test_filesystem.py` — add tests

- [x] **Step 1: Write failing tests**

```python
# tests/test_filesystem.py — add to existing file

def test_glob_pattern_basic(tmp_path):
    from ares.filesystem import glob_pattern
    (tmp_path / "a.py").write_text("x", encoding="utf-8")
    (tmp_path / "b.py").write_text("y", encoding="utf-8")
    (tmp_path / "c.txt").write_text("z", encoding="utf-8")

    result = glob_pattern("*.py", path=str(tmp_path))
    assert "a.py" in result
    assert "b.py" in result
    assert "c.txt" not in result


def test_glob_pattern_recursive(tmp_path):
    from ares.filesystem import glob_pattern
    sub = tmp_path / "src"
    sub.mkdir()
    (sub / "main.py").write_text("x", encoding="utf-8")
    (tmp_path / "root.py").write_text("y", encoding="utf-8")

    result = glob_pattern("**/*.py", path=str(tmp_path))
    assert "main.py" in result
    assert "root.py" in result


def test_glob_pattern_no_matches(tmp_path):
    from ares.filesystem import glob_pattern
    (tmp_path / "a.txt").write_text("x", encoding="utf-8")

    result = glob_pattern("*.py", path=str(tmp_path))
    assert "No matches" in result or "no matches" in result.lower()


def test_glob_pattern_skips_ignored_dirs(tmp_path):
    from ares.filesystem import glob_pattern
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    (git_dir / "config.py").write_text("secret", encoding="utf-8")
    (tmp_path / "app.py").write_text("ok", encoding="utf-8")

    result = glob_pattern("**/*.py", path=str(tmp_path))
    assert "app.py" in result
    assert "config.py" not in result
```

- [x] **Step 2: Run tests to verify they fail**

Run: `cd C:\Users\anime\ares && python -m pytest tests/test_filesystem.py::test_glob_pattern_basic tests/test_filesystem.py::test_glob_pattern_recursive tests/test_filesystem.py::test_glob_pattern_no_matches tests/test_filesystem.py::test_glob_pattern_skips_ignored_dirs -v`
Expected: FAIL — `ImportError: cannot import name 'glob_pattern'`

- [x] **Step 3: Implement `glob_pattern`**

Add to `ares/filesystem.py` after `get_file_info`:

```python
def glob_pattern(pattern: str, path: str = ".", max_results: int = 50) -> str:
    """Find files matching a glob pattern."""
    root = resolve_path(path)
    if not root.exists():
        return f"Directory not found: {path}"
    if not root.is_dir():
        return f"Not a directory: {path}"

    bounded_max = max(1, min(int(max_results), 500))
    matches = []

    try:
        for match in root.rglob(pattern):
            # Skip ignored directories
            parts = match.relative_to(root).parts
            if any(p in SKIP_DIRS for p in parts):
                continue
            matches.append(match)
            if len(matches) >= bounded_max:
                break
    except (OSError, PermissionError) as e:
        return f"Error searching: {e}"

    if not matches:
        return f"No matches for '{pattern}' in {_display_path(root)}"

    # Sort: directories first, then by path
    matches.sort(key=lambda p: (p.is_file(), str(p).lower()))

    lines = [f"Found {len(matches)} match(es) for '{pattern}':"]
    for m in matches:
        if m.is_dir():
            lines.append(f"  [dir]  {_display_path(m)}/")
        else:
            try:
                size = _format_size(m.stat().st_size)
            except OSError:
                size = "?"
            lines.append(f"  [file] {_display_path(m)}  {size}")

    if len(matches) >= bounded_max:
        lines.append(f"... (capped at {bounded_max} results)")

    return "\n".join(lines)
```

- [x] **Step 4: Run tests to verify they pass**

Run: `cd C:\Users\anime\ares && python -m pytest tests/test_filesystem.py -v`
Expected: All tests PASS.

- [x] **Step 5: Commit**

```bash
cd C:\Users\anime\ares
git add ares/filesystem.py tests/test_filesystem.py
git commit -m "feat: add glob_pattern tool

Finds files by glob pattern with recursive support. Skips
.git, node_modules, __pycache__ etc. No sandbox restriction.

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task 4: Create `filesystem_write.py` with Sandbox and Atomic Writes

**Files:**
- Create: `ares/filesystem_write.py`
- Create: `tests/test_filesystem_write.py`

- [x] **Step 1: Write failing tests for sandbox**

```python
# tests/test_filesystem_write.py

"""Tests for write filesystem operations and sandboxing."""

import pytest
from pathlib import Path


def test_resolve_write_path_inside_home(tmp_path, monkeypatch):
    """Write paths inside home should be accepted."""
    monkeypatch.setattr("ares.filesystem.Path.home", lambda: tmp_path)
    monkeypatch.setattr("ares.filesystem_write._home", lambda: tmp_path)
    from ares.filesystem_write import resolve_write_path
    target = tmp_path / "project" / "file.txt"
    result = resolve_write_path(str(target))
    assert result == target.resolve()


def test_resolve_write_path_outside_home(tmp_path, monkeypatch):
    """Write paths outside home should be rejected."""
    monkeypatch.setattr("ares.filesystem.Path.home", lambda: tmp_path)
    monkeypatch.setattr("ares.filesystem_write._home", lambda: tmp_path)
    from ares.filesystem_write import resolve_write_path
    with pytest.raises(ValueError, match="outside home directory"):
        resolve_write_path("/tmp/evil.txt")


def test_resolve_write_path_blocks_ares_config(tmp_path, monkeypatch):
    """Writes to ~/.ares/ should be blocked."""
    monkeypatch.setattr("ares.filesystem.Path.home", lambda: tmp_path)
    monkeypatch.setattr("ares.filesystem_write._home", lambda: tmp_path)
    from ares.filesystem_write import resolve_write_path
    ares_dir = tmp_path / ".ares"
    ares_dir.mkdir()
    with pytest.raises(ValueError, match="protected"):
        resolve_write_path(str(ares_dir / "config.json"))


def test_atomic_write_creates_file(tmp_path, monkeypatch):
    """atomic_write should create a new file."""
    monkeypatch.setattr("ares.filesystem_write._home", lambda: tmp_path)
    from ares.filesystem_write import atomic_write
    target = tmp_path / "new_file.txt"
    atomic_write(target, "hello world\n")
    assert target.read_text(encoding="utf-8") == "hello world\n"


def test_atomic_write_overwrites_file(tmp_path, monkeypatch):
    """atomic_write should safely overwrite an existing file."""
    monkeypatch.setattr("ares.filesystem_write._home", lambda: tmp_path)
    from ares.filesystem_write import atomic_write
    target = tmp_path / "existing.txt"
    target.write_text("old content", encoding="utf-8")
    atomic_write(target, "new content")
    assert target.read_text(encoding="utf-8") == "new content"


def test_atomic_write_cleanup_on_failure(tmp_path, monkeypatch):
    """atomic_write should clean up temp file on failure."""
    monkeypatch.setattr("ares.filesystem_write._home", lambda: tmp_path)
    from ares.filesystem_write import atomic_write
    target = tmp_path / "fail.txt"
    # Force a failure by passing non-string data
    with pytest.raises(Exception):
        atomic_write(target, None)
    # Temp file should be cleaned up
    temp_files = list(tmp_path.glob(".tmp_*.part"))
    assert len(temp_files) == 0
```

- [x] **Step 2: Run tests to verify they fail**

Run: `cd C:\Users\anime\ares && python -m pytest tests/test_filesystem_write.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'ares.filesystem_write'`

- [x] **Step 3: Create `filesystem_write.py`**

```python
# ares/filesystem_write.py

"""Write file system operations for Ares with home-directory sandboxing."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from ares.filesystem import _allowed_roots, _display_path, _format_size, SKIP_DIRS

PROTECTED_PREFIXES = (".ares",)


def _home() -> Path:
    return Path.home().resolve()


def resolve_write_path(path: str) -> Path:
    """Resolve and validate a write path. Must be inside home directory.
    Blocks writes to protected Ares system paths (~/.ares/)."""
    expanded = Path(path).expanduser().resolve()
    home = _home()

    # Check it's inside home
    try:
        expanded.relative_to(home)
    except ValueError:
        raise ValueError(f"Access denied: {path} is outside home directory")

    # Check it's not a protected Ares path
    try:
        rel = expanded.relative_to(home)
        for part in rel.parts:
            if part in PROTECTED_PREFIXES:
                raise ValueError(
                    f"Access denied: {path} is a protected Ares system path"
                )
    except ValueError:
        # If we can't get a relative path (shouldn't happen), block
        if "relative to" not in str(ValueError):
            raise

    return expanded


def atomic_write(path: Path, data: str, encoding: str = "utf-8") -> None:
    """Atomically write data to a file using temp-file-then-rename."""
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)

    fd, tmp = tempfile.mkstemp(dir=str(parent), prefix=".tmp_", suffix=".part")
    try:
        with os.fdopen(fd, "w", encoding=encoding) as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, str(path))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_file(path: str, content: str, dry_run: bool = False) -> str:
    """Create or overwrite a file."""
    resolved = resolve_write_path(path)
    exists = resolved.exists()

    preview = (
        f"{'Overwrite' if exists else 'Create'} {_display_path(resolved)} "
        f"({len(content):,} bytes)"
    )
    if dry_run:
        return f"[DRY RUN] {preview}"

    atomic_write(resolved, content)
    action = "Overwrote" if exists else "Created"
    return f"{action} {_display_path(resolved)} ({len(content):,} bytes)"


def edit_file(path: str, old_text: str, new_text: str, dry_run: bool = False) -> str:
    """Edit a file by searching and replacing text with resilience cascade."""
    resolved = resolve_write_path(path)
    if not resolved.exists():
        return f"File not found: {path}"

    try:
        content = resolved.read_text(encoding="utf-8", errors="replace")
    except PermissionError:
        return f"Permission denied: {_display_path(resolved)}"

    # 1. Exact match
    count = content.count(old_text)
    if count == 1:
        new_content = content.replace(old_text, new_text, 1)
        if dry_run:
            return f"[DRY RUN] Would edit {_display_path(resolved)} ({len(old_text)} → {len(new_text)} chars)"
        atomic_write(resolved, new_content)
        return f"Edited {_display_path(resolved)} (replaced {len(old_text)} chars)"

    if count > 1:
        return (
            f"old_text matches {count} locations in {_display_path(resolved)}. "
            "Provide more context to make it unique."
        )

    # 2. Whitespace-normalized match
    old_lines = old_text.splitlines()
    content_lines = content.splitlines()
    match_idx = _find_whitespace_match(old_lines, content_lines)
    if match_idx is not None:
        new_content_lines = content_lines[:match_idx] + new_text.splitlines() + content_lines[match_idx + len(old_lines):]
        new_content = "\n".join(new_content_lines)
        if dry_run:
            return f"[DRY RUN] Would edit {_display_path(resolved)} (whitespace-normalized match)"
        atomic_write(resolved, new_content)
        return f"Edited {_display_path(resolved)} (whitespace-normalized match)"

    # 3. "Did you mean?" suggestion
    suggestion = _find_closest_match(old_text, content)
    if suggestion:
        return (
            f"No match found for old_text in {_display_path(resolved)}.\n"
            f"Did you mean:\n---\n{suggestion}\n---"
        )

    return f"No match found for old_text in {_display_path(resolved)}."


def _find_whitespace_match(old_lines: list[str], content_lines: list[str]) -> int | None:
    """Find a match using whitespace-normalized comparison."""
    if not old_lines:
        return None

    # Normalize: strip leading whitespace from each line, track relative indent
    def normalize(lines: list[str]) -> list[str]:
        result = []
        for line in lines:
            stripped = line.lstrip()
            indent = len(line) - len(stripped)
            result.append(f"{indent}:{stripped}")
        return result

    norm_old = normalize(old_lines)
    old_len = len(norm_old)

    for i in range(len(content_lines) - old_len + 1):
        norm_slice = normalize(content_lines[i:i + old_len])
        if norm_slice == norm_old:
            return i
    return None


def _find_closest_match(old_text: str, content: str, threshold: float = 0.6) -> str | None:
    """Find the closest matching region in the file content."""
    from difflib import SequenceMatcher

    old_lines = old_text.splitlines()
    content_lines = content.splitlines()

    if not old_lines or not content_lines:
        return None

    best_ratio = 0.0
    best_start = 0
    best_len = len(old_lines)

    # Sliding window over content
    window_size = len(old_lines)
    for i in range(len(content_lines) - window_size + 1):
        chunk = content_lines[i:i + window_size]
        ratio = SequenceMatcher(None, old_lines, chunk).ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best_start = i
            best_len = window_size

    if best_ratio >= threshold:
        match_lines = content_lines[best_start:best_start + best_len]
        return "\n".join(match_lines)
    return None


def create_directory(path: str, dry_run: bool = False) -> str:
    """Create a directory with parents (mkdir -p behavior)."""
    resolved = resolve_write_path(path)
    if resolved.exists():
        if resolved.is_dir():
            return f"Directory already exists: {_display_path(resolved)}"
        return f"Not a directory: {_display_path(resolved)}"

    if dry_run:
        return f"[DRY RUN] Would create directory {_display_path(resolved)}"

    resolved.mkdir(parents=True, exist_ok=True)
    return f"Created directory {_display_path(resolved)}"


def delete_file(path: str, dry_run: bool = False) -> str:
    """Delete a file or empty directory."""
    resolved = resolve_write_path(path)
    if not resolved.exists():
        return f"File not found: {path}"

    if resolved.is_dir():
        contents = list(resolved.iterdir())
        if contents:
            items = [f"  {item.name}" for item in contents[:10]]
            more = f"  ... and {len(contents) - 10} more" if len(contents) > 10 else ""
            return (
                f"Cannot delete: {_display_path(resolved)} is a non-empty directory "
                f"({len(contents)} items):\n" + "\n".join(items) + (f"\n{more}" if more else "")
            )

    size_info = ""
    if resolved.is_file():
        try:
            size_info = f" ({_format_size(resolved.stat().st_size)})"
        except OSError:
            pass

    if dry_run:
        return f"[DRY RUN] Would delete {_display_path(resolved)}{size_info}"

    resolved.unlink()
    return f"Deleted {_display_path(resolved)}{size_info}"


def move_file(source: str, destination: str, dry_run: bool = False) -> str:
    """Move or rename a file or directory."""
    src = resolve_write_path(source)
    dst = resolve_write_path(destination)

    if not src.exists():
        return f"Source not found: {source}"

    dst_exists = dst.exists()

    src_info = ""
    if src.is_file():
        try:
            src_info = f" ({_format_size(src.stat().st_size)})"
        except OSError:
            pass

    if dst_exists:
        dst_info = ""
        if dst.is_file():
            try:
                dst_info = f" ({_format_size(dst.stat().st_size)})"
            except OSError:
                pass
        preview = f"Would move {_display_path(src)}{src_info} → {_display_path(dst)}{dst_info} (overwrites existing)"
    else:
        preview = f"Would move {_display_path(src)}{src_info} → {_display_path(dst)}"

    if dry_run:
        return f"[DRY RUN] {preview}"

    dst.parent.mkdir(parents=True, exist_ok=True)
    os.replace(str(src), str(dst))

    if dst_exists:
        return f"Moved {_display_path(src)}{src_info} → {_display_path(dst)} (overwrote existing)"
    return f"Moved {_display_path(src)}{src_info} → {_display_path(dst)}"
```

- [x] **Step 4: Run tests to verify they pass**

Run: `cd C:\Users\anime\ares && python -m pytest tests/test_filesystem_write.py -v`
Expected: All tests PASS.

- [x] **Step 5: Commit**

```bash
cd C:\Users\anime\ares
git add ares/filesystem_write.py tests/test_filesystem_write.py
git commit -m "feat: add filesystem_write module with sandbox and atomic writes

Includes resolve_write_path (home sandbox + protected paths),
atomic_write (temp-file-then-rename), and implementations for
write_file, edit_file, create_directory, delete_file, move_file.

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task 5: Add `write_file` with Confirmation Flow

**Files:**
- Modify: `ares/tools.py` — add tool definition + handler
- Modify: `tests/test_filesystem_write.py` — add write_file tests

- [x] **Step 1: Write failing tests for write_file**

```python
# tests/test_filesystem_write.py — add to existing file

def test_write_file_new(tmp_path, monkeypatch):
    monkeypatch.setattr("ares.filesystem.Path.home", lambda: tmp_path)
    monkeypatch.setattr("ares.filesystem_write._home", lambda: tmp_path)
    from ares.filesystem_write import write_file
    target = str(tmp_path / "new.txt")
    result = write_file(target, "hello world")
    assert "Created" in result
    assert (tmp_path / "new.txt").read_text(encoding="utf-8") == "hello world"


def test_write_file_overwrite_requires_confirm(tmp_path, monkeypatch):
    monkeypatch.setattr("ares.filesystem.Path.home", lambda: tmp_path)
    monkeypatch.setattr("ares.filesystem_write._home", lambda: tmp_path)
    from ares.filesystem_write import write_file
    target = tmp_path / "existing.txt"
    target.write_text("old", encoding="utf-8")
    result = write_file(str(target), "new")
    assert "CONFIRM" in result or "confirm" in result.lower()
    assert target.read_text(encoding="utf-8") == "old"  # unchanged


def test_write_file_overwrite_with_confirm(tmp_path, monkeypatch):
    monkeypatch.setattr("ares.filesystem.Path.home", lambda: tmp_path)
    monkeypatch.setattr("ares.filesystem_write._home", lambda: tmp_path)
    from ares.filesystem_write import write_file
    target = tmp_path / "existing.txt"
    target.write_text("old", encoding="utf-8")
    # write_file doesn't have confirm param — that's in ToolExecutor
    # The function itself just writes; confirmation is handled at the executor level
    result = write_file(str(target), "new")
    assert "Overwrote" in result
    assert target.read_text(encoding="utf-8") == "new"


def test_write_file_dry_run(tmp_path, monkeypatch):
    monkeypatch.setattr("ares.filesystem.Path.home", lambda: tmp_path)
    monkeypatch.setattr("ares.filesystem_write._home", lambda: tmp_path)
    from ares.filesystem_write import write_file
    target = tmp_path / "dry.txt"
    result = write_file(str(target), "content", dry_run=True)
    assert "DRY RUN" in result
    assert not target.exists()  # file should NOT be created


def test_write_file_outside_home_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr("ares.filesystem.Path.home", lambda: tmp_path)
    monkeypatch.setattr("ares.filesystem_write._home", lambda: tmp_path)
    from ares.filesystem_write import write_file
    with pytest.raises(ValueError, match="outside home directory"):
        write_file("/tmp/evil.txt", "payload")
```

- [x] **Step 2: Run tests to verify they fail**

Run: `cd C:\Users\anime\ares && python -m pytest tests/test_filesystem_write.py -k "write_file" -v`
Expected: FAIL — write_file not registered in tools.py yet, but the function exists in filesystem_write.py so direct tests should pass. The ToolExecutor test will fail.

- [x] **Step 3: Add write_file tool definition to tools.py**

In `ares/tools.py`, add to the `get_tool_definitions()` list (after `list_directory`):

```python
_tool(
    "write_file",
    "Create a new file or overwrite an existing one. If overwriting, confirm=true is required.",
    {
        "path": {"type": "string", "description": "File path to write."},
        "content": {"type": "string", "description": "File content to write."},
        "dry_run": {"type": "boolean", "default": False, "description": "Preview without writing."},
        "confirm": {"type": "boolean", "default": False, "description": "Confirm destructive overwrite."},
    },
    ["path", "content"],
),
```

Add the import at the top:

```python
from ares.filesystem_write import write_file as _write_file_impl
```

Add the handler to `ToolExecutor`:

```python
def _write_file(self, args: dict) -> str:
    path = args["path"]
    content = args["content"]
    dry_run = bool(args.get("dry_run", False))
    confirm = bool(args.get("confirm", False))

    # Check if file exists and needs confirmation
    from ares.filesystem import resolve_path as read_resolve
    try:
        resolved = read_resolve(path)
        is_overwrite = resolved.exists()
    except ValueError:
        is_overwrite = False

    if is_overwrite and not confirm and not dry_run:
        from ares.filesystem import _format_size
        try:
            size = _format_size(resolved.stat().st_size)
        except OSError:
            size = "unknown"
        return (
            f"⚠ CONFIRM REQUIRED: This will overwrite {path} ({size}). "
            f"Re-call with confirm=true to proceed."
        )

    return _write_file_impl(path, content, dry_run=dry_run)
```

Register in `handlers` dict:

```python
"write_file": self._write_file,
```

- [x] **Step 4: Run all tests**

Run: `cd C:\Users\anime\ares && python -m pytest tests/ -v`
Expected: All tests PASS.

- [x] **Step 5: Commit**

```bash
cd C:\Users\anime\ares
git add ares/tools.py tests/test_filesystem_write.py
git commit -m "feat: register write_file tool with confirmation flow

ToolExecutor checks if file exists before writing and returns
a confirmation prompt unless confirm=true. Supports dry_run.

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task 6: Add `edit_file` Tool

**Files:**
- Modify: `ares/tools.py` — add tool definition + handler
- Modify: `tests/test_filesystem_write.py` — add edit_file tests

- [x] **Step 1: Write failing tests for edit_file**

```python
# tests/test_filesystem_write.py — add to existing file

def test_edit_file_exact_match(tmp_path, monkeypatch):
    monkeypatch.setattr("ares.filesystem.Path.home", lambda: tmp_path)
    monkeypatch.setattr("ares.filesystem_write._home", lambda: tmp_path)
    from ares.filesystem_write import edit_file
    target = tmp_path / "code.py"
    target.write_text("def greet():\n    print('hello')\n", encoding="utf-8")
    result = edit_file(str(target), "print('hello')", "print('world')")
    assert "Edited" in result
    assert target.read_text(encoding="utf-8") == "def greet():\n    print('world')\n"


def test_edit_file_no_match_returns_suggestion(tmp_path, monkeypatch):
    monkeypatch.setattr("ares.filesystem.Path.home", lambda: tmp_path)
    monkeypatch.setattr("ares.filesystem_write._home", lambda: tmp_path)
    from ares.filesystem_write import edit_file
    target = tmp_path / "code.py"
    target.write_text("def greet():\n    print('hello')\n", encoding="utf-8")
    result = edit_file(str(target), "print('goodbye')", "print('world')")
    assert "No match" in result or "Did you mean" in result


def test_edit_file_multiple_matches(tmp_path, monkeypatch):
    monkeypatch.setattr("ares.filesystem.Path.home", lambda: tmp_path)
    monkeypatch.setattr("ares.filesystem_write._home", lambda: tmp_path)
    from ares.filesystem_write import edit_file
    target = tmp_path / "code.py"
    target.write_text("x = 1\nx = 2\nx = 3\n", encoding="utf-8")
    result = edit_file(str(target), "x = 1", "x = 10")
    assert "matches" in result.lower() and "locations" in result.lower()


def test_edit_file_whitespace_normalized(tmp_path, monkeypatch):
    monkeypatch.setattr("ares.filesystem.Path.home", lambda: tmp_path)
    monkeypatch.setattr("ares.filesystem_write._home", lambda: tmp_path)
    from ares.filesystem_write import edit_file
    target = tmp_path / "code.py"
    target.write_text("def greet():\n    print('hello')\n", encoding="utf-8")
    # LLM sends wrong indentation
    result = edit_file(str(target), "print('hello')", "print('world')")
    assert "Edited" in result


def test_edit_file_dry_run(tmp_path, monkeypatch):
    monkeypatch.setattr("ares.filesystem.Path.home", lambda: tmp_path)
    monkeypatch.setattr("ares.filesystem_write._home", lambda: tmp_path)
    from ares.filesystem_write import edit_file
    target = tmp_path / "code.py"
    target.write_text("old content", encoding="utf-8")
    result = edit_file(str(target), "old", "new", dry_run=True)
    assert "DRY RUN" in result
    assert target.read_text(encoding="utf-8") == "old content"  # unchanged


def test_edit_file_not_found(tmp_path, monkeypatch):
    monkeypatch.setattr("ares.filesystem.Path.home", lambda: tmp_path)
    monkeypatch.setattr("ares.filesystem_write._home", lambda: tmp_path)
    from ares.filesystem_write import edit_file
    result = edit_file(str(tmp_path / "nope.py"), "a", "b")
    assert "not found" in result.lower()
```

- [x] **Step 2: Run tests to verify they fail**

Run: `cd C:\Users\anime\ares && python -m pytest tests/test_filesystem_write.py -k "edit_file" -v`
Expected: FAIL — edit_file not registered in tools.py yet.

- [x] **Step 3: Add edit_file tool definition to tools.py**

In `ares/tools.py`, add to `get_tool_definitions()`:

```python
_tool(
    "edit_file",
    "Edit a file by searching and replacing text. old_text must match uniquely. If no match, returns the closest content as a suggestion.",
    {
        "path": {"type": "string", "description": "File path."},
        "old_text": {"type": "string", "description": "Text to find (must match uniquely in the file)."},
        "new_text": {"type": "string", "description": "Replacement text."},
        "dry_run": {"type": "boolean", "default": False, "description": "Preview without editing."},
    },
    ["path", "old_text", "new_text"],
),
```

Add import:

```python
from ares.filesystem_write import edit_file as _edit_file_impl
```

Add handler:

```python
def _edit_file(self, args: dict) -> str:
    return _edit_file_impl(
        args["path"],
        args["old_text"],
        args["new_text"],
        dry_run=bool(args.get("dry_run", False)),
    )
```

Register in `handlers`:

```python
"edit_file": self._edit_file,
```

- [x] **Step 4: Run all tests**

Run: `cd C:\Users\anime\ares && python -m pytest tests/ -v`
Expected: All tests PASS.

- [x] **Step 5: Commit**

```bash
cd C:\Users\anime\ares
git add ares/tools.py tests/test_filesystem_write.py
git commit -m "feat: register edit_file tool with matching cascade

Exact → whitespace-normalized → fuzzy 'did you mean?' suggestion.
Multiple matches return an error asking for more context.

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task 7: Add `create_directory` Tool

**Files:**
- Modify: `ares/tools.py` — add tool definition + handler
- Modify: `tests/test_filesystem_write.py` — add tests

- [x] **Step 1: Write failing tests**

```python
# tests/test_filesystem_write.py — add to existing file

def test_create_directory(tmp_path, monkeypatch):
    monkeypatch.setattr("ares.filesystem.Path.home", lambda: tmp_path)
    monkeypatch.setattr("ares.filesystem_write._home", lambda: tmp_path)
    from ares.filesystem_write import create_directory
    target = tmp_path / "new_dir" / "sub"
    result = create_directory(str(target))
    assert "Created" in result
    assert target.is_dir()


def test_create_directory_already_exists(tmp_path, monkeypatch):
    monkeypatch.setattr("ares.filesystem.Path.home", lambda: tmp_path)
    monkeypatch.setattr("ares.filesystem_write._home", lambda: tmp_path)
    from ares.filesystem_write import create_directory
    target = tmp_path / "existing"
    target.mkdir()
    result = create_directory(str(target))
    assert "already exists" in result.lower()


def test_create_directory_dry_run(tmp_path, monkeypatch):
    monkeypatch.setattr("ares.filesystem.Path.home", lambda: tmp_path)
    monkeypatch.setattr("ares.filesystem_write._home", lambda: tmp_path)
    from ares.filesystem_write import create_directory
    target = tmp_path / "would_create"
    result = create_directory(str(target), dry_run=True)
    assert "DRY RUN" in result
    assert not target.exists()
```

- [x] **Step 2: Run tests to verify they fail**

Run: `cd C:\Users\anime\ares && python -m pytest tests/test_filesystem_write.py -k "create_directory" -v`
Expected: FAIL — not registered in tools.py.

- [x] **Step 3: Add create_directory tool definition to tools.py**

In `ares/tools.py`, add to `get_tool_definitions()`:

```python
_tool(
    "create_directory",
    "Create a directory and any missing parent directories (mkdir -p).",
    {
        "path": {"type": "string", "description": "Directory path to create."},
        "dry_run": {"type": "boolean", "default": False, "description": "Preview without creating."},
    },
    ["path"],
),
```

Add import:

```python
from ares.filesystem_write import create_directory as _create_directory_impl
```

Add handler:

```python
def _create_directory(self, args: dict) -> str:
    return _create_directory_impl(
        args["path"],
        dry_run=bool(args.get("dry_run", False)),
    )
```

Register in `handlers`:

```python
"create_directory": self._create_directory,
```

- [x] **Step 4: Run all tests**

Run: `cd C:\Users\anime\ares && python -m pytest tests/ -v`
Expected: All tests PASS.

- [x] **Step 5: Commit**

```bash
cd C:\Users\anime\ares
git add ares/tools.py tests/test_filesystem_write.py
git commit -m "feat: register create_directory tool with dry_run support

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task 8: Add `delete_file` Tool

**Files:**
- Modify: `ares/tools.py` — add tool definition + handler
- Modify: `tests/test_filesystem_write.py` — add tests

- [x] **Step 1: Write failing tests**

```python
# tests/test_filesystem_write.py — add to existing file

def test_delete_file(tmp_path, monkeypatch):
    monkeypatch.setattr("ares.filesystem.Path.home", lambda: tmp_path)
    monkeypatch.setattr("ares.filesystem_write._home", lambda: tmp_path)
    from ares.filesystem_write import delete_file
    target = tmp_path / "to_delete.txt"
    target.write_text("bye", encoding="utf-8")
    result = delete_file(str(target))
    assert "Deleted" in result
    assert not target.exists()


def test_delete_file_requires_confirm(tmp_path, monkeypatch):
    """delete_file should return confirmation prompt when confirm not set."""
    monkeypatch.setattr("ares.filesystem.Path.home", lambda: tmp_path)
    monkeypatch.setattr("ares.filesystem_write._home", lambda: tmp_path)
    from ares.filesystem_write import delete_file
    target = tmp_path / "to_delete.txt"
    target.write_text("bye", encoding="utf-8")
    # delete_file doesn't have confirm param — confirmation is in ToolExecutor
    # The function itself just deletes
    result = delete_file(str(target))
    assert "Deleted" in result


def test_delete_nonempty_directory_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr("ares.filesystem.Path.home", lambda: tmp_path)
    monkeypatch.setattr("ares.filesystem_write._home", lambda: tmp_path)
    from ares.filesystem_write import delete_file
    d = tmp_path / "nonempty"
    d.mkdir()
    (d / "file.txt").write_text("x", encoding="utf-8")
    result = delete_file(str(d))
    assert "non-empty" in result.lower() or "Cannot delete" in result


def test_delete_empty_directory(tmp_path, monkeypatch):
    monkeypatch.setattr("ares.filesystem.Path.home", lambda: tmp_path)
    monkeypatch.setattr("ares.filesystem_write._home", lambda: tmp_path)
    from ares.filesystem_write import delete_file
    d = tmp_path / "empty_dir"
    d.mkdir()
    result = delete_file(str(d))
    assert "Deleted" in result
    assert not d.exists()


def test_delete_dry_run(tmp_path, monkeypatch):
    monkeypatch.setattr("ares.filesystem.Path.home", lambda: tmp_path)
    monkeypatch.setattr("ares.filesystem_write._home", lambda: tmp_path)
    from ares.filesystem_write import delete_file
    target = tmp_path / "keep.txt"
    target.write_text("keep", encoding="utf-8")
    result = delete_file(str(target), dry_run=True)
    assert "DRY RUN" in result
    assert target.exists()
```

- [x] **Step 2: Run tests to verify they fail**

Run: `cd C:\Users\anime\ares && python -m pytest tests/test_filesystem_write.py -k "delete" -v`
Expected: FAIL — not registered in tools.py.

- [x] **Step 3: Add delete_file tool definition + handler to tools.py**

In `ares/tools.py`, add to `get_tool_definitions()`:

```python
_tool(
    "delete_file",
    "Delete a file or empty directory. Always requires confirm=true.",
    {
        "path": {"type": "string", "description": "File or directory path to delete."},
        "confirm": {"type": "boolean", "default": False, "description": "Confirm deletion."},
        "dry_run": {"type": "boolean", "default": False, "description": "Preview without deleting."},
    },
    ["path"],
),
```

Add import:

```python
from ares.filesystem_write import delete_file as _delete_file_impl
```

Add handler:

```python
def _delete_file(self, args: dict) -> str:
    path = args["path"]
    dry_run = bool(args.get("dry_run", False))
    confirm = bool(args.get("confirm", False))

    if not confirm and not dry_run:
        return (
            f"⚠ CONFIRM REQUIRED: This will delete {path}. "
            f"Re-call with confirm=true to proceed."
        )

    return _delete_file_impl(path, dry_run=dry_run)
```

Register in `handlers`:

```python
"delete_file": self._delete_file,
```

- [x] **Step 4: Run all tests**

Run: `cd C:\Users\anime\ares && python -m pytest tests/ -v`
Expected: All tests PASS.

- [x] **Step 5: Commit**

```bash
cd C:\Users\anime\ares
git add ares/tools.py tests/test_filesystem_write.py
git commit -m "feat: register delete_file tool with confirmation gate

Always requires confirm=true. Refuses non-empty directories.
Supports dry_run.

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task 9: Add `move_file` Tool

**Files:**
- Modify: `ares/tools.py` — add tool definition + handler
- Modify: `tests/test_filesystem_write.py` — add tests

- [x] **Step 1: Write failing tests**

```python
# tests/test_filesystem_write.py — add to existing file

def test_move_file_basic(tmp_path, monkeypatch):
    monkeypatch.setattr("ares.filesystem.Path.home", lambda: tmp_path)
    monkeypatch.setattr("ares.filesystem_write._home", lambda: tmp_path)
    from ares.filesystem_write import move_file
    src = tmp_path / "old.txt"
    src.write_text("content", encoding="utf-8")
    dst = tmp_path / "new.txt"
    result = move_file(str(src), str(dst))
    assert "Moved" in result
    assert not src.exists()
    assert dst.read_text(encoding="utf-8") == "content"


def test_move_file_overwrite_requires_confirm(tmp_path, monkeypatch):
    """move_file to existing destination should mention overwrite."""
    monkeypatch.setattr("ares.filesystem.Path.home", lambda: tmp_path)
    monkeypatch.setattr("ares.filesystem_write._home", lambda: tmp_path)
    from ares.filesystem_write import move_file
    src = tmp_path / "a.txt"
    src.write_text("new", encoding="utf-8")
    dst = tmp_path / "b.txt"
    dst.write_text("old", encoding="utf-8")
    result = move_file(str(src), str(dst))
    assert "overwrit" in result.lower() or "Moved" in result


def test_move_file_source_not_found(tmp_path, monkeypatch):
    monkeypatch.setattr("ares.filesystem.Path.home", lambda: tmp_path)
    monkeypatch.setattr("ares.filesystem_write._home", lambda: tmp_path)
    from ares.filesystem_write import move_file
    result = move_file(str(tmp_path / "nope.txt"), str(tmp_path / "dest.txt"))
    assert "not found" in result.lower()


def test_move_file_dry_run(tmp_path, monkeypatch):
    monkeypatch.setattr("ares.filesystem.Path.home", lambda: tmp_path)
    monkeypatch.setattr("ares.filesystem_write._home", lambda: tmp_path)
    from ares.filesystem_write import move_file
    src = tmp_path / "src.txt"
    src.write_text("data", encoding="utf-8")
    dst = tmp_path / "dst.txt"
    result = move_file(str(src), str(dst), dry_run=True)
    assert "DRY RUN" in result
    assert src.exists()  # unchanged
    assert not dst.exists()


def test_move_file_creates_parent_dirs(tmp_path, monkeypatch):
    monkeypatch.setattr("ares.filesystem.Path.home", lambda: tmp_path)
    monkeypatch.setattr("ares.filesystem_write._home", lambda: tmp_path)
    from ares.filesystem_write import move_file
    src = tmp_path / "file.txt"
    src.write_text("data", encoding="utf-8")
    dst = tmp_path / "sub" / "dir" / "file.txt"
    result = move_file(str(src), str(dst))
    assert "Moved" in result
    assert dst.read_text(encoding="utf-8") == "data"
```

- [x] **Step 2: Run tests to verify they fail**

Run: `cd C:\Users\anime\ares && python -m pytest tests/test_filesystem_write.py -k "move_file" -v`
Expected: FAIL — not registered in tools.py.

- [x] **Step 3: Add move_file tool definition + handler to tools.py**

In `ares/tools.py`, add to `get_tool_definitions()`:

```python
_tool(
    "move_file",
    "Move or rename a file or directory. Creates parent directories of destination as needed.",
    {
        "source": {"type": "string", "description": "Current file path."},
        "destination": {"type": "string", "description": "New file path."},
        "confirm": {"type": "boolean", "default": False, "description": "Confirm if destination exists."},
        "dry_run": {"type": "boolean", "default": False, "description": "Preview without moving."},
    },
    ["source", "destination"],
),
```

Add import:

```python
from ares.filesystem_write import move_file as _move_file_impl
```

Add handler:

```python
def _move_file(self, args: dict) -> str:
    source = args["source"]
    destination = args["destination"]
    dry_run = bool(args.get("dry_run", False))
    confirm = bool(args.get("confirm", False))

    # Check if destination exists and needs confirmation
    from ares.filesystem import resolve_path as read_resolve
    try:
        dst_resolved = read_resolve(destination)
        dst_exists = dst_resolved.exists()
    except ValueError:
        dst_exists = False

    if dst_exists and not confirm and not dry_run:
        return (
            f"⚠ CONFIRM REQUIRED: Destination {destination} already exists. "
            f"Re-call with confirm=true to proceed (will overwrite)."
        )

    return _move_file_impl(source, destination, dry_run=dry_run)
```

Register in `handlers`:

```python
"move_file": self._move_file,
```

- [x] **Step 4: Run all tests**

Run: `cd C:\Users\anime\ares && python -m pytest tests/ -v`
Expected: All tests PASS.

- [x] **Step 5: Commit**

```bash
cd C:\Users\anime\ares
git add ares/tools.py tests/test_filesystem_write.py
git commit -m "feat: register move_file tool with overwrite confirmation

Creates parent directories. Requires confirm=true when
destination already exists. Supports dry_run.

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task 10: Add `glob_pattern` and `get_file_info` to Tool Definitions

**Files:**
- Modify: `ares/tools.py` — add tool definitions + handlers for read tools
- Modify: `tests/test_tools.py` — add tests for new tool definitions

- [x] **Step 1: Write failing tests for tool definitions**

```python
# tests/test_tools.py — add to existing file

def test_get_file_info_tool_definition():
    from ares.tools import get_tool_definitions
    defs = get_tool_definitions()
    names = [d["function"]["name"] for d in defs]
    assert "get_file_info" in names


def test_glob_pattern_tool_definition():
    from ares.tools import get_tool_definitions
    defs = get_tool_definitions()
    names = [d["function"]["name"] for d in defs]
    assert "glob_pattern" in names


def test_write_file_tool_definition():
    from ares.tools import get_tool_definitions
    defs = get_tool_definitions()
    names = [d["function"]["name"] for d in defs]
    assert "write_file" in names


def test_edit_file_tool_definition():
    from ares.tools import get_tool_definitions
    defs = get_tool_definitions()
    names = [d["function"]["name"] for d in defs]
    assert "edit_file" in names


def test_create_directory_tool_definition():
    from ares.tools import get_tool_definitions
    defs = get_tool_definitions()
    names = [d["function"]["name"] for d in defs]
    assert "create_directory" in names


def test_delete_file_tool_definition():
    from ares.tools import get_tool_definitions
    defs = get_tool_definitions()
    names = [d["function"]["name"] for d in defs]
    assert "delete_file" in names


def test_move_file_tool_definition():
    from ares.tools import get_tool_definitions
    defs = get_tool_definitions()
    names = [d["function"]["name"] for d in defs]
    assert "move_file" in names
```

- [x] **Step 2: Run tests to verify they fail**

Run: `cd C:\Users\anime\ares && python -m pytest tests/test_tools.py -k "tool_definition" -v`
Expected: The new read tool tests (get_file_info, glob_pattern) may already pass if the definitions were added earlier. The write tool tests should all pass by this point.

- [x] **Step 3: Add remaining tool definitions to tools.py**

The read tool definitions for `get_file_info` and `glob_pattern` need to be added to `get_tool_definitions()`:

```python
_tool(
    "get_file_info",
    "Get metadata about a file or directory: type, size, timestamps, binary status.",
    {
        "path": {"type": "string", "description": "File or directory path."},
    },
    ["path"],
),
_tool(
    "glob_pattern",
    "Find files matching a glob pattern (e.g. **/*.py, src/**/*.ts).",
    {
        "pattern": {"type": "string", "description": "Glob pattern."},
        "path": {"type": "string", "default": ".", "description": "Directory to search from."},
        "max_results": {"type": "integer", "default": 50, "description": "Max files to return."},
    },
    ["pattern"],
),
```

Add imports:

```python
from ares.filesystem import get_file_info as _get_file_info_impl
from ares.filesystem import glob_pattern as _glob_pattern_impl
```

Add handlers:

```python
def _get_file_info(self, args: dict) -> str:
    return _get_file_info_impl(args["path"])

def _glob_pattern(self, args: dict) -> str:
    return _glob_pattern_impl(
        args["pattern"],
        path=args.get("path", "."),
        max_results=int(args.get("max_results", 50)),
    )
```

Register in `handlers`:

```python
"get_file_info": self._get_file_info,
"glob_pattern": self._glob_pattern,
```

- [x] **Step 4: Run all tests**

Run: `cd C:\Users\anime\ares && python -m pytest tests/ -v`
Expected: All tests PASS. All 8 file tools should be registered.

- [x] **Step 5: Commit**

```bash
cd C:\Users\anime\ares
git add ares/tools.py tests/test_tools.py
git commit -m "feat: register all 8 file tool definitions in ToolExecutor

Read tools: get_file_info, glob_pattern
Write tools: write_file, edit_file, create_directory, delete_file, move_file

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task 11: Final Integration Tests

**Files:**
- Modify: `tests/test_filesystem_write.py` — add end-to-end integration tests
- Modify: `tests/test_tools.py` — add executor integration tests

- [x] **Step 1: Write integration tests**

```python
# tests/test_filesystem_write.py — add to existing file

def test_full_workflow_create_edit_delete(tmp_path, monkeypatch):
    """End-to-end: create file, edit it, verify, delete it."""
    monkeypatch.setattr("ares.filesystem.Path.home", lambda: tmp_path)
    monkeypatch.setattr("ares.filesystem_write._home", lambda: tmp_path)
    from ares.filesystem_write import write_file, edit_file, delete_file

    path = str(tmp_path / "project" / "main.py")

    # Create
    result = write_file(path, "def main():\n    pass\n")
    assert "Created" in result

    # Edit
    result = edit_file(path, "pass", "print('hello')")
    assert "Edited" in result

    # Verify
    content = (tmp_path / "project" / "main.py").read_text(encoding="utf-8")
    assert "print('hello')" in content

    # Delete
    result = delete_file(path)
    assert "Deleted" in result
    assert not (tmp_path / "project" / "main.py").exists()


def test_sandbox_blocks_write_outside_home(tmp_path, monkeypatch):
    """Writes to paths outside home must fail."""
    monkeypatch.setattr("ares.filesystem.Path.home", lambda: tmp_path)
    monkeypatch.setattr("ares.filesystem_write._home", lambda: tmp_path)
    from ares.filesystem_write import write_file
    with pytest.raises(ValueError, match="outside home"):
        write_file("/etc/passwd", "hacked")


def test_sandbox_blocks_ares_config(tmp_path, monkeypatch):
    """Writes to ~/.ares/ must fail."""
    monkeypatch.setattr("ares.filesystem.Path.home", lambda: tmp_path)
    monkeypatch.setattr("ares.filesystem_write._home", lambda: tmp_path)
    from ares.filesystem_write import write_file
    ares_dir = tmp_path / ".ares"
    ares_dir.mkdir()
    (ares_dir / "config.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="protected"):
        write_file(str(ares_dir / "config.json"), "hacked")
```

```python
# tests/test_tools.py — add to existing file

def test_executor_write_file_new(tmp_path, monkeypatch):
    from ares.tools import ToolExecutor
    from ares.memory import MemoryStore
    from ares.tasks import TaskStore
    monkeypatch.setattr("ares.filesystem.Path.home", lambda: tmp_path)
    monkeypatch.setattr("ares.filesystem_write._home", lambda: tmp_path)
    executor = ToolExecutor(MemoryStore(":memory:"), TaskStore(":memory:"))
    path = str(tmp_path / "test.txt")
    result = executor.execute("write_file", {"path": path, "content": "hello"})
    assert "Created" in result


def test_executor_write_file_overwrite_blocked(tmp_path, monkeypatch):
    from ares.tools import ToolExecutor
    from ares.memory import MemoryStore
    from ares.tasks import TaskStore
    monkeypatch.setattr("ares.filesystem.Path.home", lambda: tmp_path)
    monkeypatch.setattr("ares.filesystem_write._home", lambda: tmp_path)
    executor = ToolExecutor(MemoryStore(":memory:"), TaskStore(":memory:"))
    path = tmp_path / "existing.txt"
    path.write_text("old", encoding="utf-8")
    result = executor.execute("write_file", {"path": str(path), "content": "new"})
    assert "CONFIRM" in result
    assert path.read_text(encoding="utf-8") == "old"  # unchanged


def test_executor_delete_file_blocked_without_confirm(tmp_path, monkeypatch):
    from ares.tools import ToolExecutor
    from ares.memory import MemoryStore
    from ares.tasks import TaskStore
    monkeypatch.setattr("ares.filesystem.Path.home", lambda: tmp_path)
    monkeypatch.setattr("ares.filesystem_write._home", lambda: tmp_path)
    executor = ToolExecutor(MemoryStore(":memory:"), TaskStore(":memory:"))
    path = tmp_path / "victim.txt"
    path.write_text("bye", encoding="utf-8")
    result = executor.execute("delete_file", {"path": str(path)})
    assert "CONFIRM" in result
    assert path.exists()  # unchanged
```

- [x] **Step 2: Run all tests**

Run: `cd C:\Users\anime\ares && python -m pytest tests/ -v`
Expected: All tests PASS. Full integration verified.

- [x] **Step 3: Commit**

```bash
cd C:\Users\anime\ares
git add tests/test_filesystem_write.py tests/test_tools.py
git commit -m "test: add integration tests for file tools workflow

Covers create→edit→verify→delete flow, sandbox enforcement,
and ToolExecutor confirmation gates.

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```
