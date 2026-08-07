"""Read-only file system operations for Ares."""

from __future__ import annotations

import asyncio
import fnmatch
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

MAX_READ_LINES = 2000
MAX_SEARCH_RESULTS = 100
MAX_RESULT_EXCERPT = 500
SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv", ".pytest_cache"}

# Hard ceilings so a misbehaving search can never hang the assistant for
# minutes the way an unbounded os.walk into a Windows junction (node_modules,
# OneDrive, openwiki, …) used to.  These bound the *fallback* Python walker;
# ripgrep runs under its own timeout instead.
_SEARCH_MAX_DEPTH = 40
_SEARCH_MAX_FILES = 200_000
_SEARCH_MAX_SECONDS = 20
_RG_TIMEOUT_SECONDS = 30


def _has_ripgrep() -> bool:
    """Cache whether the fast ripgrep ('rg') binary is on PATH."""
    cached = getattr(_has_ripgrep, "_cache", None)
    if cached is None:
        cached = shutil.which("rg") is not None
        _has_ripgrep._cache = cached  # type: ignore[attr-defined]
    return cached


def _is_junction(path: Path) -> bool:
    """True for Windows reparse points (junctions, but also symlinks).

    Python's ``os.walk`` only skips *symlinks* via ``followlinks=False``; it
    happily recurses into junctions, which is what made ``~\Downloads\Telegram
    Desktop`` scans take 100s+.  Detect them explicitly so we never descend.
    """
    if sys.platform != "win32":
        return False
    try:
        return path.is_junction()
    except (OSError, NotImplementedError, AttributeError):
        try:
            st = os.lstat(path)
        except OSError:
            return False
        # FILE_ATTRIBUTE_REPARSE_POINT == 0x400
        return bool(getattr(st, "st_file_attributes", 0) & 0x400)


def _allowed_roots() -> list[Path]:
    return [Path.home().resolve()]


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _normalize_path(path: str) -> str:
    r"""Normalize path for cross-platform compatibility.
    
    Handles Unix-style paths on Windows (e.g., /c/Users -> C:/Users)
    and normalizes separators.
    """
    import sys
    if not path:
        return "."
    
    # On Windows, convert Unix-style drive paths
    if sys.platform == "win32":
        # Handle /c/Users -> C:\Users
        if len(path) >= 3 and path[0] == "/" and path[2] == "/" and path[1].isalpha():
            drive = path[1].upper()
            rest = path[2:].replace("/", "\\")
            path = f"{drive}:{rest}"
        # Handle //c/Users -> \\c\Users (UNC-like)
        elif len(path) >= 4 and path.startswith("//") and path[2].isalpha() and path[3] == "/":
            drive = path[2].upper()
            rest = path[3:].replace("/", "\\")
            path = f"\\\\{drive}:{rest}"
    
    return path


def resolve_path(path: str = ".") -> Path:
    """Resolve a path. No access restrictions for reads.
    
    Handles Windows paths, Unix-style paths, ~ expansion, and relative paths.
    """
    normalized = _normalize_path(path or ".")
    return Path(normalized).expanduser().resolve()


def _lexical_path(path: str = ".") -> Path:
    """Make a path absolute without resolving its final symlink."""
    normalized = _normalize_path(path or ".")
    return Path(os.path.abspath(os.fspath(Path(normalized).expanduser())))


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
    """Read a bounded window without retaining a whole large file in memory."""
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
    end_requested = start + bounded_num_lines - 1
    selected: list[tuple[int, str]] = []
    context = _stream_symbol_context(resolved, start)
    total = 0
    try:
        with open(resolved, encoding="utf-8", errors="replace") as handle:
            for line_number, line in enumerate(handle, 1):
                total = line_number
                if start <= line_number <= end_requested:
                    selected.append((line_number, line.rstrip()))
    except PermissionError:
        return f"Permission denied: {_display_path(resolved)}"

    end = selected[-1][0] if selected else min(start - 1, total)
    parts = [f"[File: {_display_path(resolved)} ({total} lines total)]"]
    if context:
        parts.append("Context:")
        parts.extend(context)
    if start > 1:
        parts.append(f"({min(start - 1, total)} more lines above)")
    for line_number, line in selected:
        parts.append(f"{line_number:>6}\t{line}")
    if end < total:
        parts.append(f"({total - end} more lines below)")
    return "\n".join(parts)


def _stream_symbol_context(path: Path, start: int) -> list[str]:
    """Collect a tiny, useful context window while keeping memory bounded."""
    suffix = path.suffix.lower()
    if suffix not in {".py", ".js", ".jsx", ".ts", ".tsx", ".md"} or start <= 1:
        return []
    imports: list[tuple[int, str]] = []
    last_scope: tuple[int, str] | None = None
    last_class: tuple[int, str] | None = None
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            for line_number, line in enumerate(handle, 1):
                if line_number >= start:
                    break
                stripped = line.strip()
                if suffix == ".py":
                    if line_number <= 80 and stripped.startswith(("import ", "from ")):
                        imports.append((line_number, line.rstrip()))
                    if line.lstrip().startswith("class "):
                        last_class = (line_number, line.rstrip())
                    if re.match(r"(async\s+def|def|class)\s+", line.lstrip()):
                        last_scope = (line_number, line.rstrip())
                elif suffix in {".js", ".jsx", ".ts", ".tsx"}:
                    if line_number <= 80 and stripped.startswith(("import ", "const ", "let ", "var ")) and " from " in stripped:
                        imports.append((line_number, line.rstrip()))
                    if re.match(r"(export\s+)?(async\s+)?function\s+|class\s+|const\s+\w+\s*=", stripped):
                        last_scope = (line_number, line.rstrip())
                elif suffix == ".md" and stripped.startswith("#"):
                    last_scope = (line_number, line.rstrip())
    except OSError:
        return []
    context = [f"  import {line_no}: {line}" for line_no, line in imports[:6]]
    if last_class and last_scope != last_class:
        context.append(f"  scope {last_class[0]}: {last_class[1]}")
    if last_scope:
        label = "heading" if suffix == ".md" else "scope"
        context.append(f"  {label} {last_scope[0]}: {last_scope[1]}")
    return context[:8]


def _symbol_context(path: Path, lines: list[str], start: int, end: int) -> list[str]:
    """Return nearby imports/classes/functions for code-oriented file slices."""
    suffix = path.suffix.lower()
    if suffix not in {".py", ".js", ".jsx", ".ts", ".tsx", ".md"}:
        return []

    selected_range = set(range(start, end + 1))
    context: list[str] = []
    seen: set[int] = set()

    def add(line_no: int, label: str) -> None:
        if line_no in seen or line_no in selected_range:
            return
        seen.add(line_no)
        context.append(f"  {label} {line_no}: {lines[line_no - 1].rstrip()}")

    if suffix == ".py":
        for i, line in enumerate(lines[: min(len(lines), 80)], 1):
            stripped = line.strip()
            if stripped.startswith(("import ", "from ")) and i < start:
                add(i, "import")
        for i in range(start - 1, 0, -1):
            line = lines[i - 1]
            stripped = line.lstrip()
            if re.match(r"(async\s+def|def|class)\s+", stripped):
                add(i, "scope")
                if stripped.startswith("def ") or stripped.startswith("async def "):
                    indent = len(line) - len(stripped)
                    for j in range(i - 1, 0, -1):
                        parent = lines[j - 1]
                        parent_stripped = parent.lstrip()
                        parent_indent = len(parent) - len(parent_stripped)
                        if parent_indent < indent and parent_stripped.startswith("class "):
                            add(j, "scope")
                            break
                break
    elif suffix in {".js", ".jsx", ".ts", ".tsx"}:
        for i, line in enumerate(lines[: min(len(lines), 80)], 1):
            stripped = line.strip()
            if stripped.startswith(("import ", "const ", "let ", "var ")) and " from " in stripped and i < start:
                add(i, "import")
        for i in range(start - 1, 0, -1):
            stripped = lines[i - 1].strip()
            if re.match(r"(export\s+)?(async\s+)?function\s+|class\s+|const\s+\w+\s*=", stripped):
                add(i, "scope")
                break
    elif suffix == ".md":
        for i in range(start - 1, 0, -1):
            if lines[i - 1].lstrip().startswith("#"):
                add(i, "heading")
                break

    return context[:8]


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


def _load_ignore_patterns(root: Path) -> list[str]:
    """Load simple .gitignore-style patterns for Python fallback paths."""
    patterns: list[str] = []
    for name in (".gitignore", ".ignore"):
        ignore_file = root / name
        if not ignore_file.exists():
            continue
        try:
            for raw in ignore_file.read_text(encoding="utf-8", errors="replace").splitlines():
                line = raw.strip()
                if not line or line.startswith("#") or line.startswith("!"):
                    continue
                patterns.append(line)
        except OSError:
            continue
    return patterns


def _is_ignored(path: Path, root: Path, ignore_patterns: list[str] | None = None) -> bool:
    """Return true for noisy dirs and simple .gitignore pattern matches."""
    try:
        rel = path.relative_to(root)
    except ValueError:
        rel = path
    parts = rel.parts
    if any(part in SKIP_DIRS for part in parts):
        return True
    patterns = ignore_patterns if ignore_patterns is not None else _load_ignore_patterns(root)
    rel_posix = rel.as_posix()
    for pattern in patterns:
        anchored = pattern.startswith("/")
        pat = pattern.lstrip("/")
        if pattern.endswith("/"):
            prefix = pat.rstrip("/")
            if any(part == prefix for part in parts) or rel_posix.startswith(prefix + "/"):
                return True
            continue
        if anchored:
            if fnmatch.fnmatch(rel_posix, pat):
                return True
        elif "/" in pat:
            if fnmatch.fnmatch(rel_posix, pat) or fnmatch.fnmatch(rel_posix, f"*/{pat}"):
                return True
        elif fnmatch.fnmatch(path.name, pat) or any(fnmatch.fnmatch(part, pat) for part in parts):
            return True
    return False


def _attach_context(results: list[dict], root: Path, radius: int = 1) -> list[dict]:
    """Attach one-line snippets around content matches where possible."""
    for result in results:
        if result.get("match_type") != "content":
            continue
        path = Path(result["path"])
        try:
            if _is_binary(path):
                continue
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        line_no = int(result.get("line") or 0)
        if line_no < 1:
            continue
        snippets = []
        for show_no in range(max(1, line_no - radius), min(len(lines), line_no + radius) + 1):
            marker = ">" if show_no == line_no else " "
            snippets.append(f"{marker}{show_no}: {lines[show_no - 1].strip()[:MAX_RESULT_EXCERPT]}")
        result["context"] = snippets
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
        "--max-depth",
        "30",
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
        stdout, _stderr = await asyncio.wait_for(proc.communicate(), timeout=_RG_TIMEOUT_SECONDS)
    except (FileNotFoundError, asyncio.TimeoutError):
        return []

    if proc.returncode not in (0, 1):
        return []
    return _attach_context(_parse_ripgrep_output(stdout.decode(errors="replace")), path)


def _iter_files(root: Path, name_pattern: str = "", *, max_depth: int = _SEARCH_MAX_DEPTH):
    """Yield files under root while skipping noisy + junction directories.

    Bounded by depth, total file count, and wall-clock time so a recursive walk
    can never hang the assistant — an unbounded os.walk into a Windows junction
    (node_modules, OneDrive, openwiki, …) previously ran for 100s+.
    """
    ignore_patterns = _load_ignore_patterns(root)
    root_len = len(str(root))
    deadline = time.monotonic() + _SEARCH_MAX_SECONDS
    yielded = 0
    for current_root, dirs, files in os.walk(root, followlinks=False):
        depth = current_root[root_len:].count(os.sep)
        if depth > max_depth:
            dirs[:] = []
            continue
        kept = []
        for d in dirs:
            child = Path(current_root) / d
            if _is_junction(child):
                continue
            if _is_ignored(child, root, ignore_patterns):
                continue
            kept.append(d)
        dirs[:] = kept
        if time.monotonic() > deadline:
            break
        for filename in files:
            file_path = Path(current_root) / filename
            if _is_ignored(file_path, root, ignore_patterns):
                continue
            if name_pattern and not fnmatch.fnmatch(filename, name_pattern):
                continue
            yield file_path
            yielded += 1
            if yielded >= _SEARCH_MAX_FILES:
                return


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
    """Search files by glob name, preferring ripgrep when available."""
    if _has_ripgrep():
        try:
            proc = await asyncio.create_subprocess_exec(
                "rg", "--files", "--hidden",
                "-g", pattern or "*",
                # Mirror the Python fallback's always-skipped noisy dirs so the
                # two paths return the same set (the fallback never consults
                # .gitignore for these). rg still respects .gitignore/.ignore and
                # always excludes .git on its own.
                "--glob", "!node_modules", "--glob", "!.venv", "--glob", "!venv",
                "--glob", "!__pycache__", "--glob", "!.pytest_cache",
                "--max-depth", "30",
                str(path),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            stdout, _stderr = await asyncio.wait_for(proc.communicate(), timeout=_RG_TIMEOUT_SECONDS)
            if proc.returncode in (0, 1):
                lines = stdout.decode(errors="replace").splitlines()
                if lines:
                    return [
                        {"path": line, "line": 0, "excerpt": "", "match_type": "name"}
                        for line in lines[:max_results]
                    ]
        except (OSError, asyncio.TimeoutError):
            pass
    results = []
    for file_path in _iter_files(path, pattern, max_depth=30):
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
    content_hits = [result for result in results if result["match_type"] == "content"]
    matched_files = {str(Path(result["path"]).resolve()) for result in results}
    lines = [
        f"Found {total_count} file result(s) across {len(matched_files)} matched file(s) "
        f"and {len(content_hits)} matched line(s):"
    ]
    for result in results:
        path = _display_path(Path(result["path"]))
        if result["match_type"] == "content":
            lines.append(f"[content match] {path}:{result['line']}")
            lines.append(f"  {result['excerpt']}")
            for snippet in result.get("context", []):
                lines.append(f"  {snippet}")
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
    merged: dict[tuple[str, int, str], dict] = {}

    if query.strip():
        for result in await _content_search(query, root, name_pattern):
            key = (str(Path(result["path"]).resolve()), int(result.get("line") or 0), result["match_type"])
            merged.setdefault(key, result)

    if name_pattern.strip():
        for result in await _name_search(name_pattern, root):
            key = (str(Path(result["path"]).resolve()), int(result.get("line") or 0), result["match_type"])
            merged.setdefault(key, result)

    results = list(merged.values())
    results.sort(key=lambda item: _search_rank(item, query, name_pattern))
    return _format_search_results(results[:bounded_max], total=len(results))


def _search_rank(item: dict, query: str, name_pattern: str) -> tuple:
    """Stable ranking: content hits, filename relevance, then path/line."""
    path = Path(item["path"])
    haystack = f"{path.name}\n{item.get('excerpt', '')}".lower()
    query_l = query.lower().strip()
    score = 0
    if item["match_type"] == "content":
        score -= 100
    if query_l and query_l in path.name.lower():
        score -= 25
    if name_pattern and fnmatch.fnmatch(path.name, name_pattern):
        score -= 5
    if query_l and haystack.startswith(query_l):
        score -= 10
    return (score, str(path).lower(), int(item.get("line") or 0))


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


def _format_timestamp(ts: float) -> str:
    """Format a timestamp as a human-readable string."""
    from datetime import datetime, timezone
    try:
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    except (OSError, ValueError):
        return "unknown"


def get_file_info(path: str) -> str:
    """Get lexical metadata, including valid and broken symbolic links."""
    lexical = _lexical_path(path)
    try:
        stat = lexical.lstat()
    except FileNotFoundError:
        return f"File not found: {path}"
    except PermissionError:
        return f"Permission denied: {path}"
    except OSError as exc:
        return f"Error reading file info: {exc}"

    is_link = lexical.is_symlink()
    if is_link:
        ftype = "symlink"
    elif lexical.is_dir():
        ftype = "directory"
    elif lexical.is_file():
        ftype = "file"
    else:
        ftype = "unknown"
    size = _format_size(stat.st_size)
    lines = [
        f"[File Info: {_display_path(lexical)}]",
        f"  Type: {ftype}",
        f"  Size: {size} ({stat.st_size:,} bytes)",
        f"  Modified: {_format_timestamp(stat.st_mtime)}",
        f"  Accessed: {_format_timestamp(stat.st_atime)}",
        f"  Created: {_format_timestamp(stat.st_ctime)}",
    ]
    if is_link:
        try:
            target_text = os.readlink(lexical)
            target_path = (lexical.parent / target_text).resolve(strict=False)
            lines.append(f"  Link target: {target_text}")
            lines.append(f"  Target status: {'exists' if target_path.exists() else 'broken'}")
            lines.append(f"  Resolved target: {target_path}")
        except OSError as exc:
            lines.append(f"  Link target: unavailable ({exc})")
    elif lexical.is_file():
        try:
            lines.append(f"  Binary: {'yes' if _is_binary(lexical) else 'no'}")
        except (OSError, PermissionError):
            lines.append("  Binary: unknown")
    return "\n".join(lines)


def glob_pattern(pattern: str, path: str = ".", max_results: int = 50, max_depth: int = 8) -> str:
    """Find files matching a glob pattern (ripgrep when available)."""
    root = resolve_path(path)
    if not root.exists():
        return f"Directory not found: {path}"
    if not root.is_dir():
        return f"Not a directory: {path}"

    bounded_max = max(1, min(int(max_results), 500))
    matches: list[Path] = []

    if _has_ripgrep():
        try:
            proc = subprocess.run(
                ["rg", "--files", "-g", pattern, "--max-depth", str(max_depth), str(root)],
                capture_output=True, text=True, timeout=_RG_TIMEOUT_SECONDS,
            )
            if proc.returncode in (0, 1):
                for line in proc.stdout.splitlines():
                    p = Path(line)
                    if _is_ignored(p, root):
                        continue
                    matches.append(p)
                    if len(matches) >= bounded_max:
                        break
        except (OSError, subprocess.TimeoutExpired):
            matches = []

    if not matches:
        # Bounded, junction-safe Python fallback.
        for match in _iter_files(root, pattern, max_depth=max_depth):
            if _is_ignored(match, root):
                continue
            matches.append(match)
            if len(matches) >= bounded_max:
                break

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


def disk_usage(path: str = ".", max_depth: int = 2) -> str:
    """Show disk usage from one bottom-up filesystem walk."""
    root = resolve_path(path)
    if not root.exists():
        return f"Path not found: {path}"
    if root.is_file():
        try:
            size = root.stat().st_size
        except OSError as exc:
            return f"Error reading file size: {exc}"
        return f"[Disk Usage: {_display_path(root)}]\n  File: {_format_size(size)} ({size:,} bytes)"
    if not root.is_dir():
        return f"Not a directory: {path}"

    depth = max(1, min(int(max_depth), 5))
    ignored = _load_ignore_patterns(root)
    direct_sizes: dict[Path, int] = {root: 0}
    direct_counts: dict[Path, int] = {root: 0}
    children: dict[Path, list[Path]] = {root: []}
    files: dict[Path, list[tuple[str, int]]] = {root: []}
    for current_text, dirs, filenames in os.walk(root):
        current = Path(current_text)
        direct_sizes.setdefault(current, 0)
        direct_counts.setdefault(current, 0)
        children.setdefault(current, [])
        files.setdefault(current, [])
        kept_dirs = []
        for directory_name in dirs:
            child = current / directory_name
            if _is_ignored(child, root, ignored):
                continue
            kept_dirs.append(directory_name)
            children[current].append(child)
            direct_sizes.setdefault(child, 0)
            direct_counts.setdefault(child, 0)
            children.setdefault(child, [])
            files.setdefault(child, [])
        dirs[:] = kept_dirs
        for filename in filenames:
            file_path = current / filename
            if _is_ignored(file_path, root, ignored):
                continue
            try:
                # This is intentionally the only stat performed for each file.
                size = file_path.stat().st_size
            except (OSError, PermissionError):
                continue
            direct_sizes[current] += size
            direct_counts[current] += 1
            files[current].append((filename, size))

    totals = dict(direct_sizes)
    counts = dict(direct_counts)
    for directory in sorted(totals, key=lambda item: len(item.parts), reverse=True):
        for child in children.get(directory, []):
            totals[directory] += totals.get(child, 0)
            counts[directory] += counts.get(child, 0)

    lines = [f"[Disk Usage: {_display_path(root)}]", f"  Total: {_format_size(totals[root])} ({counts[root]:,} files)", ""]

    def render(directory: Path, current_depth: int, prefix: str = "") -> None:
        if current_depth > depth:
            return
        for child in sorted(children.get(directory, []), key=lambda item: item.name.casefold())[:20]:
            lines.append(f"{prefix}{child.name}/  {_format_size(totals.get(child, 0))} ({counts.get(child, 0)} files)")
            render(child, current_depth + 1, prefix + "  ")
        for name, size in sorted(files.get(directory, []), key=lambda item: item[0].casefold())[:50]:
            lines.append(f"{prefix}{name}  {_format_size(size)}")

    render(root, 1)
    return "\n".join(lines)


def checksum(path: str, algorithm: str = "sha256") -> str:
    """Compute file checksum (md5, sha1, sha256, sha512)."""
    import hashlib

    resolved = resolve_path(path)
    if not resolved.exists():
        return f"File not found: {path}"
    if not resolved.is_file():
        return f"Not a file: {path}"

    algo = algorithm.lower().replace("-", "")
    if algo not in ("md5", "sha1", "sha256", "sha512"):
        return f"Unsupported algorithm: {algorithm}. Use md5, sha1, sha256, or sha512."

    try:
        h = hashlib.new(algo)
        with open(resolved, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                h.update(chunk)
        hash_hex = h.hexdigest()

        size = _format_size(resolved.stat().st_size)
        return (
            f"[Checksum: {_display_path(resolved)}]\n"
            f"  Algorithm: {algo.upper()}\n"
            f"  Hash: {hash_hex}\n"
            f"  Size: {size}"
        )
    except PermissionError:
        return f"Permission denied: {_display_path(resolved)}"
    except OSError as e:
        return f"Error computing checksum: {e}"


def copy_file(source: str, destination: str, overwrite: bool = False, dry_run: bool = False) -> str:
    """Copy a file atomically, preserve metadata, and verify bytes."""
    src = resolve_path(source)
    if not src.exists():
        return f"Source not found: {source}"
    if not src.is_file():
        return f"Not a file: {source}"

    dst = resolve_path(destination)
    if dst.exists() and not overwrite:
        dst_size = _format_size(dst.stat().st_size) if dst.is_file() else "dir"
        return (
            f"Destination exists ({dst_size}). "
            f"Use overwrite=true to replace."
        )

    src_size = _format_size(src.stat().st_size)
    preview = f"Copy {_display_path(src)} ({src_size}) → {_display_path(dst)}"

    if dry_run:
        return f"[DRY RUN] {preview}"

    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temp_name = tempfile.mkstemp(prefix=f".{dst.stem}.", suffix=dst.suffix or ".part", dir=dst.parent)
        temporary = Path(temp_name)
        try:
            source_digest = hashlib.sha256()
            copied_digest = hashlib.sha256()
            source_size = 0
            with os.fdopen(descriptor, "wb") as target, src.open("rb") as origin:
                while True:
                    chunk = origin.read(1024 * 1024)
                    if not chunk:
                        break
                    source_size += len(chunk)
                    source_digest.update(chunk)
                    target.write(chunk)
                target.flush()
                os.fsync(target.fileno())
            with temporary.open("rb") as copied:
                while True:
                    chunk = copied.read(1024 * 1024)
                    if not chunk:
                        break
                    copied_digest.update(chunk)
            if temporary.stat().st_size != source_size or copied_digest.digest() != source_digest.digest():
                raise OSError("Copied bytes did not verify against the source")
            shutil.copystat(src, temporary, follow_symlinks=True)
            os.replace(temporary, dst)
        finally:
            try:
                temporary.unlink()
            except (FileNotFoundError, UnboundLocalError):
                pass
        return f"{preview} (verified SHA-256)"
    except PermissionError:
        return f"Permission denied: {_display_path(dst)}"
    except OSError as e:
        return f"Error copying file: {e}"


def find_duplicates(path: str = ".", min_size: int = 1024, max_results: int = 50) -> str:
    """Find duplicate files by size and hash.
    
    Scans directory for files with identical size and content hash.
    Useful for finding redundant files to clean up.
    """
    root = resolve_path(path)
    if not root.exists():
        return f"Path not found: {path}"
    if not root.is_dir():
        return f"Not a directory: {path}"

    size_map: dict[int, list[Path]] = {}

    # Group files by size using the same ignore policy as search_files.
    for p in _iter_files(root):
        try:
            size = p.stat().st_size
            if size >= min_size:
                size_map.setdefault(size, []).append(p)
        except (OSError, PermissionError):
            continue

    def sample_digest(file_path: Path, size: int) -> str:
        sample = 64 * 1024
        digest = hashlib.sha256()
        with file_path.open("rb") as handle:
            digest.update(handle.read(sample))
            if size > sample:
                handle.seek(max(0, size - sample))
                digest.update(handle.read(sample))
        return digest.hexdigest()

    def full_digest(file_path: Path) -> str:
        digest = hashlib.sha256()
        with file_path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    # Size -> samples -> full SHA-256 avoids reading full same-size but
    # unrelated files, while retaining exact duplicate results.
    dup_groups: list[tuple[int, str, list[Path]]] = []
    for size, files in size_map.items():
        if len(files) < 2:
            continue
        sample_map: dict[str, list[Path]] = {}
        for f in files:
            try:
                sample_map.setdefault(sample_digest(f, size), []).append(f)
            except (OSError, PermissionError):
                continue
        for candidates in sample_map.values():
            if len(candidates) < 2:
                continue
            hash_map: dict[str, list[Path]] = {}
            for candidate in candidates:
                try:
                    hash_map.setdefault(full_digest(candidate), []).append(candidate)
                except (OSError, PermissionError):
                    continue
            for digest, dup_files in hash_map.items():
                if len(dup_files) >= 2:
                    dup_groups.append((size, digest, dup_files))

    if not dup_groups:
        return "No duplicate files found."

    dup_groups.sort(key=lambda x: -x[0])  # largest first
    lines = [f"[Duplicate Files in {_display_path(root)}]"]

    shown = 0
    for size, h, files in dup_groups:
        if shown >= max_results:
            lines.append(f"... and {len(dup_groups) - shown} more duplicate groups")
            break
        lines.append(f"\n  {_format_size(size)} (sha256: {h[:8]}...):")
        for f in files[:10]:
            lines.append(f"    {_display_path(f)}")
        if len(files) > 10:
            lines.append(f"    ... and {len(files) - 10} more")
        shown += 1

    total_dup_size = sum(s for s, _, files in dup_groups for _ in files[1:])
    lines.insert(1, f"  Found {len(dup_groups)} duplicate groups, {_format_size(total_dup_size)} recoverable")

    return "\n".join(lines)


def tail_file(path: str, num_lines: int = 20) -> str:
    """Read the last N lines of a file (like Unix tail)."""
    resolved = resolve_path(path)
    if not resolved.exists():
        return f"File not found: {path}"
    if not resolved.is_file():
        return f"Not a file: {path}"

    bounded = max(1, min(int(num_lines), 500))

    try:
        with open(resolved, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except PermissionError:
        return f"Permission denied: {_display_path(resolved)}"

    total = len(lines)
    start = max(0, total - bounded)
    selected = lines[start:]

    parts = [f"[Tail: {_display_path(resolved)} (last {len(selected)} of {total} lines)]"]
    for i, line in enumerate(selected, start + 1):
        parts.append(f"{i:>6}\t{line.rstrip()}")

    return "\n".join(parts)


def head_file(path: str, num_lines: int = 20) -> str:
    """Read the first N lines of a file (like Unix head)."""
    resolved = resolve_path(path)
    if not resolved.exists():
        return f"File not found: {path}"
    if not resolved.is_file():
        return f"Not a file: {path}"

    bounded = max(1, min(int(num_lines), 500))

    try:
        with open(resolved, encoding="utf-8", errors="replace") as f:
            lines = []
            for i, line in enumerate(f):
                if i >= bounded:
                    break
                lines.append(line)
    except PermissionError:
        return f"Permission denied: {_display_path(resolved)}"

    total_lines = 0
    try:
        with open(resolved, encoding="utf-8", errors="replace") as f:
            total_lines = sum(1 for _ in f)
    except (OSError, PermissionError):
        pass

    parts = [f"[Head: {_display_path(resolved)} (first {len(lines)} of {total_lines} lines)]"]
    for i, line in enumerate(lines, 1):
        parts.append(f"{i:>6}\t{line.rstrip()}")

    if total_lines > len(lines):
        parts.append(f"({total_lines - len(lines)} more lines below)")

    return "\n".join(parts)


def count_lines(path: str = "", pattern: str = "", name_pattern: str = "") -> str:
    """Count lines in files, optionally filtered by content pattern and file name.
    
    Like 'wc -l' with optional grep filtering.
    """
    root = resolve_path(path or ".")
    if not root.exists():
        return f"Path not found: {path}"
    if not root.is_dir():
        return f"Not a directory: {path}"

    content_pattern = None
    if pattern:
        try:
            content_pattern = re.compile(pattern, re.IGNORECASE)
        except re.error:
            content_pattern = re.compile(re.escape(pattern), re.IGNORECASE)

    total_lines = 0
    total_files = 0
    file_counts: list[tuple[str, int]] = []

    for p in root.rglob("*"):
        if not p.is_file():
            continue
        parts = p.relative_to(root).parts
        if any(part in SKIP_DIRS for part in parts):
            continue
        if name_pattern and not fnmatch.fnmatch(p.name, name_pattern):
            continue

        try:
            with open(p, encoding="utf-8", errors="replace") as f:
                if content_pattern:
                    count = sum(1 for line in f if content_pattern.search(line))
                else:
                    count = sum(1 for _ in f)
            if count > 0:
                total_lines += count
                total_files += 1
                file_counts.append((_display_path(p), count))
        except (OSError, PermissionError):
            continue

    file_counts.sort(key=lambda x: -x[1])

    lines = [f"[Line Count: {_display_path(root)}]"]
    lines.append(f"  Total: {total_lines:,} lines in {total_files} files")

    if pattern:
        lines.append(f"  Matching pattern: '{pattern}'")
    if name_pattern:
        lines.append(f"  File filter: '{name_pattern}'")

    lines.append("")
    for path_str, count in file_counts[:30]:
        lines.append(f"  {count:>8}  {path_str}")

    if len(file_counts) > 30:
        lines.append(f"  ... and {len(file_counts) - 30} more files")

    return "\n".join(lines)


def file_tree(path: str = ".", max_depth: int = 3, show_files: bool = True) -> str:
    """Display directory tree structure (like tree command)."""
    root = resolve_path(path)
    if not root.exists():
        return f"Path not found: {path}"
    if not root.is_dir():
        return f"Not a directory: {path}"

    depth = max(1, min(int(max_depth), 10))

    def _build_tree(p: Path, current_depth: int, prefix: str = "") -> list[str]:
        lines = []
        if current_depth > depth:
            return lines

        try:
            items = sorted(p.iterdir(), key=lambda x: (x.is_file(), x.name.lower()))
        except (PermissionError, OSError):
            return [f"{prefix}[access denied]"]

        items = [i for i in items if not _is_ignored(i, root)]
        if not show_files:
            items = [i for i in items if i.is_dir()]

        for i, item in enumerate(items):
            is_last = (i == len(items) - 1)
            connector = "└── " if is_last else "├── "
            child_prefix = "    " if is_last else "│   "

            if item.is_dir():
                if _is_junction(item):
                    continue
                name = item.name + "/"
                lines.append(f"{prefix}{connector}{name}")
                if current_depth < depth:
                    lines.extend(_build_tree(item, current_depth + 1, prefix + child_prefix))
            else:
                if show_files:
                    lines.append(f"{prefix}{connector}{item.name}")

        return lines

    lines = [f"[Tree: {_display_path(root)}]"]
    lines.append(root.name + "/")
    lines.extend(_build_tree(root, 1))

    return "\n".join(lines)
