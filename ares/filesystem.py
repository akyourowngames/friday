"""Read-only file system operations for Ares."""

from __future__ import annotations

import asyncio
import fnmatch
import os
import re
import threading
from pathlib import Path

MAX_READ_LINES = 2000
MAX_SEARCH_RESULTS = 100
MAX_RESULT_EXCERPT = 500
SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv", ".pytest_cache"}


def _allowed_roots() -> list[Path]:
    return [Path.home().resolve()]


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def resolve_path(path: str = ".") -> Path:
    """Resolve and validate a path. Access is limited to the home directory."""
    expanded = Path(path or ".").expanduser().resolve()
    roots = _allowed_roots()
    if not any(_is_relative_to(expanded, root) for root in roots):
        raise ValueError(f"Access denied: {path} is outside home directory")
    return expanded


def _is_binary(path: Path, check_bytes: int = 1024) -> bool:
    """Detect binary files by checking for null bytes in the first N bytes."""
    with open(path, "rb") as f:
        return b"\x00" in f.read(check_bytes)


def _format_size(size: int) -> str:
    """Format file size in human-readable form."""
    if size < 1024:
        return f"{size}B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f}KB"
    return f"{size / (1024 * 1024):.1f}MB"


def _display_path(path: Path) -> str:
    """Return a compact display path, relative to home when possible."""
    for root in _allowed_roots():
        try:
            rel = path.resolve().relative_to(root)
            return str(Path("~") / rel)
        except ValueError:
            continue
    return str(path)


def read_file(path: str, start_line: int = 1, num_lines: int = 200) -> str:
    """Read a text file with line numbers and truncation indicators."""
    resolved = resolve_path(path)
    if not resolved.exists():
        raise FileNotFoundError(f"File not found: {path}")
    if not resolved.is_file():
        return f"Not a file: {path}"

    try:
        if _is_binary(resolved):
            return f"Binary file - cannot display content: {_display_path(resolved)}"
    except PermissionError:
        return f"Permission denied: {_display_path(resolved)}"

    bounded_num_lines = max(1, min(int(num_lines), MAX_READ_LINES))
    start = max(1, int(start_line))

    try:
        with open(resolved, encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()
    except PermissionError:
        return f"Permission denied: {_display_path(resolved)}"

    total = len(all_lines)
    end = min(start + bounded_num_lines - 1, total)
    selected = all_lines[start - 1 : end]

    parts = [f"[File: {_display_path(resolved)} ({total} lines total)]"]
    if start > 1:
        parts.append(f"({start - 1} more lines above)")

    for line_number, line in enumerate(selected, start):
        parts.append(f"{line_number:>6}\t{line.rstrip()}")

    if end < total:
        parts.append(f"({total - end} more lines below)")

    return "\n".join(parts)


def _parse_ripgrep_output(output: str) -> list[dict]:
    """Parse ripgrep's path:line:excerpt output."""
    results = []
    for line in output.splitlines():
        match = re.match(r"^(.+?):(\d+):(.*)$", line)
        if not match:
            continue
        results.append({
            "path": match.group(1),
            "line": int(match.group(2)),
            "excerpt": match.group(3).strip(),
            "match_type": "content",
        })
    return results


async def _content_search_ripgrep(
    query: str,
    path: Path,
    name_pattern: str = "",
) -> list[dict]:
    """Search file contents with ripgrep."""
    cmd = [
        "rg",
        "-n",
        "--max-columns",
        str(MAX_RESULT_EXCERPT),
        "--max-count",
        "3",
        "-i",
    ]
    if name_pattern:
        cmd.extend(["-g", name_pattern])
    cmd.extend([query, str(path)])

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _stderr = await proc.communicate()
    except FileNotFoundError:
        return []

    if proc.returncode not in (0, 1):
        return []
    return _parse_ripgrep_output(stdout.decode(errors="replace"))


def _iter_files(root: Path, name_pattern: str = ""):
    """Yield files under root while skipping noisy directories."""
    for current_root, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for filename in files:
            if name_pattern and not fnmatch.fnmatch(filename, name_pattern):
                continue
            yield Path(current_root) / filename


async def _content_search_python(
    query: str,
    path: Path,
    name_pattern: str = "",
    max_results: int = MAX_SEARCH_RESULTS,
) -> list[dict]:
    """Fallback content search using Python regex."""
    results = []
    try:
        pattern = re.compile(query, re.IGNORECASE)
    except re.error:
        pattern = re.compile(re.escape(query), re.IGNORECASE)

    for file_path in _iter_files(path, name_pattern):
        try:
            if _is_binary(file_path):
                continue
            with open(file_path, encoding="utf-8", errors="replace") as f:
                for line_number, line in enumerate(f, 1):
                    if pattern.search(line):
                        results.append({
                            "path": str(file_path),
                            "line": line_number,
                            "excerpt": line.strip()[:MAX_RESULT_EXCERPT],
                            "match_type": "content",
                        })
                        if len(results) >= max_results:
                            return results
        except (OSError, PermissionError, re.error):
            continue
    return results


async def _content_search(query: str, path: Path, name_pattern: str = "") -> list[dict]:
    """Search file contents using ripgrep first, then Python fallback."""
    results = await _content_search_ripgrep(query, path, name_pattern)
    if results:
        return results
    return await _content_search_python(query, path, name_pattern)


async def _name_search(pattern: str, path: Path, max_results: int = MAX_SEARCH_RESULTS) -> list[dict]:
    """Search files by glob name."""
    results = []
    for file_path in _iter_files(path, pattern):
        results.append({
            "path": str(file_path),
            "line": 0,
            "excerpt": "",
            "match_type": "name",
        })
        if len(results) >= max_results:
            break
    return results


def _format_search_results(results: list[dict], total: int | None = None) -> str:
    """Format file search results for LLM consumption."""
    if not results:
        return "No results found."

    total_count = total if total is not None else len(results)
    lines = [f"Found {total_count} file result(s):"]
    for result in results:
        path = _display_path(Path(result["path"]))
        if result["match_type"] == "content":
            lines.append(f"[content match] {path}:{result['line']}")
            lines.append(f"  {result['excerpt']}")
        else:
            lines.append(f"[name match] {path}")
    if total_count > len(results):
        lines.append(f"... and {total_count - len(results)} more result(s)")
    return "\n".join(lines)


async def search_files_async(
    query: str = "",
    path: str = ".",
    name_pattern: str = "",
    max_results: int = 20,
) -> str:
    """Search files by content and/or name."""
    root = resolve_path(path)
    if not root.exists():
        raise FileNotFoundError(f"Directory not found: {path}")
    if root.is_file():
        root = root.parent
    if not root.is_dir():
        return f"Not a directory: {path}"

    bounded_max = max(1, min(int(max_results), 100))
    merged: dict[str, dict] = {}

    if query.strip():
        for result in await _content_search(query, root, name_pattern):
            merged.setdefault(str(Path(result["path"]).resolve()), result)

    if name_pattern.strip():
        for result in await _name_search(name_pattern, root):
            merged.setdefault(str(Path(result["path"]).resolve()), result)

    results = list(merged.values())
    results.sort(key=lambda item: (item["match_type"] != "content", item["path"], item["line"]))
    return _format_search_results(results[:bounded_max], total=len(results))


def search_files(
    query: str = "",
    path: str = ".",
    name_pattern: str = "",
    max_results: int = 20,
) -> str:
    """Synchronous wrapper for file search."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(search_files_async(query, path, name_pattern, max_results))

    result: dict[str, str | BaseException] = {}

    def runner() -> None:
        try:
            result["value"] = asyncio.run(search_files_async(query, path, name_pattern, max_results))
        except BaseException as exc:
            result["error"] = exc

    thread = threading.Thread(target=runner)
    thread.start()
    thread.join()
    if "error" in result:
        raise result["error"]
    return str(result.get("value", ""))


def list_directory(path: str = ".", max_items: int = 30) -> str:
    """List directory contents with file sizes."""
    resolved = resolve_path(path)
    if not resolved.exists():
        raise FileNotFoundError(f"Directory not found: {path}")
    if not resolved.is_dir():
        return f"Not a directory: {path}"

    try:
        items = sorted(resolved.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
    except PermissionError:
        return f"Permission denied: {_display_path(resolved)}"

    bounded_max = max(1, min(int(max_items), 200))
    lines = [f"[Directory: {_display_path(resolved)}]"]
    for item in items[:bounded_max]:
        if item.is_dir():
            lines.append(f"  [dir]  {item.name}/")
        else:
            try:
                size = _format_size(item.stat().st_size)
            except OSError:
                size = "unknown"
            lines.append(f"  [file] {item.name}  {size}")

    if len(items) > bounded_max:
        lines.append(f"... and {len(items) - bounded_max} more item(s)")

    return "\n".join(lines)
