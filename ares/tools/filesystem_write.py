"""Write file system operations for Ares."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from ares.tools.filesystem import _allowed_roots, _display_path, _format_size, _normalize_path, SKIP_DIRS


def resolve_write_path(path: str) -> Path:
    """Resolve a write path. No access restrictions."""
    normalized = _normalize_path(path)
    expanded = Path(normalized).expanduser().resolve()
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

    if resolved.is_dir():
        resolved.rmdir()
    else:
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
