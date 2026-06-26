"""Write file system operations for Ares."""

from __future__ import annotations

import difflib
import os
import shutil
import tempfile
from datetime import datetime, timezone
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


def batch_edit(operations: list[dict], dry_run: bool = False, confirm: bool = False, max_operations: int = 100) -> str:
    """Execute multiple file operations sequentially with per-operation reporting.

    Supported actions: write, edit, delete, move, copy, mkdir/create_directory.
    Destructive actions (delete, overwrite writes/moves/copies) require confirm=true
    unless dry_run=true.
    """
    if not isinstance(operations, list):
        return "operations must be a list."
    bounded = max(1, min(int(max_operations), 500))
    if len(operations) > bounded:
        return f"Too many operations: {len(operations)} requested, max is {bounded}."

    results: list[str] = []
    errors = 0
    for index, operation in enumerate(operations, 1):
        if not isinstance(operation, dict):
            errors += 1
            results.append(f"{index}. Error: operation must be an object")
            continue
        action = str(operation.get("action", "")).lower().strip()
        op_dry_run = bool(operation.get("dry_run", dry_run))
        try:
            if action == "write":
                path = operation["path"]
                content = operation.get("content", "")
                target = resolve_write_path(path)
                if target.exists() and not confirm and not op_dry_run:
                    raise ValueError(f"confirm=true required to overwrite {_display_path(target)}")
                result = write_file(path, content, dry_run=op_dry_run)
            elif action == "edit":
                result = edit_file(
                    operation["path"],
                    operation["old_text"],
                    operation["new_text"],
                    dry_run=op_dry_run,
                )
            elif action == "delete":
                if not confirm and not op_dry_run:
                    raise ValueError("confirm=true required for delete")
                result = delete_file(operation["path"], dry_run=op_dry_run)
            elif action == "move":
                dst = resolve_write_path(operation["destination"])
                if dst.exists() and not confirm and not op_dry_run:
                    raise ValueError(f"confirm=true required to overwrite {_display_path(dst)}")
                result = move_file(operation["source"], operation["destination"], dry_run=op_dry_run)
            elif action == "copy":
                src = resolve_write_path(operation["source"])
                dst = resolve_write_path(operation["destination"])
                if not src.exists():
                    raise FileNotFoundError(f"Source not found: {operation['source']}")
                if dst.exists() and not confirm and not op_dry_run:
                    raise ValueError(f"confirm=true required to overwrite {_display_path(dst)}")
                preview = f"Copy {_display_path(src)} → {_display_path(dst)}"
                if op_dry_run:
                    result = f"[DRY RUN] Would {preview.lower()}"
                else:
                    import shutil
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    if src.is_dir():
                        shutil.copytree(src, dst, dirs_exist_ok=confirm)
                    else:
                        shutil.copy2(src, dst)
                    result = preview
            elif action in {"mkdir", "create_directory"}:
                result = create_directory(operation["path"], dry_run=op_dry_run)
            else:
                raise ValueError(f"Unsupported action: {action or '<missing>'}")
            results.append(f"{index}. {result}")
        except Exception as exc:
            errors += 1
            results.append(f"{index}. Error: {exc}")

    status = "completed" if errors == 0 else f"completed with {errors} error(s)"
    return f"Batch edit {status}: {len(operations)} operation(s)\n" + "\n".join(results)


def glob_apply(
    pattern: str,
    action: str = "list",
    path: str = ".",
    destination: str = "",
    replacement: str = "",
    dry_run: bool = True,
    confirm: bool = False,
    max_matches: int = 100,
) -> str:
    """Apply a bulk action to files matching a glob pattern.

    Supported actions: list, delete, move, copy. Destructive actions require
    confirm=true; dry_run defaults to true for safety.
    """
    root = resolve_write_path(path)
    if not root.exists():
        return f"Directory not found: {path}"
    if not root.is_dir():
        return f"Not a directory: {path}"

    bounded = max(1, min(int(max_matches), 500))
    matches: list[Path] = []
    try:
        for match in root.rglob(pattern):
            parts = match.relative_to(root).parts
            if any(part in SKIP_DIRS for part in parts):
                continue
            matches.append(match)
            if len(matches) >= bounded:
                break
    except (OSError, PermissionError) as exc:
        return f"Error searching: {exc}"

    matches.sort(key=lambda p: str(p).lower())
    if not matches:
        return f"No matches for '{pattern}' in {_display_path(root)}"

    action = action.lower().strip()
    if action == "list":
        lines = [f"Found {len(matches)} match(es) for '{pattern}':"]
        lines.extend(f"  {_display_path(match)}" for match in matches)
        return "\n".join(lines)

    if action in {"delete", "move", "copy"} and not (confirm or dry_run):
        return f"confirm=true required for glob {action}; re-run with dry_run=true to preview."

    operations: list[dict] = []
    for match in matches:
        if action == "delete":
            operations.append({"action": "delete", "path": str(match)})
        elif action in {"move", "copy"}:
            if not destination:
                return f"destination is required for glob {action}."
            dest_root = resolve_write_path(destination)
            dest = dest_root / match.relative_to(root)
            if replacement:
                dest = dest_root / replacement.format(name=match.name, stem=match.stem, suffix=match.suffix)
            operations.append({"action": action, "source": str(match), "destination": str(dest)})
        else:
            return f"Unsupported glob action: {action}"

    return batch_edit(operations, dry_run=dry_run, confirm=confirm, max_operations=bounded)

# ── Safer line-based file manager tools ─────────────────────────────

DANGEROUS_WRITE_ROOTS = (
    Path("/bin"), Path("/boot"), Path("/dev"), Path("/etc"), Path("/lib"),
    Path("/lib64"), Path("/proc"), Path("/root"), Path("/sbin"), Path("/sys"),
    Path("/usr"), Path("/var"),
)
TEMPLATES = {
    "python": "#!/usr/bin/env python3\n\n\ndef main() -> None:\n    pass\n\n\nif __name__ == \"__main__\":\n    main()\n",
    "html": "<!doctype html>\n<html lang=\"en\">\n<head>\n  <meta charset=\"utf-8\">\n  <title>New Page</title>\n</head>\n<body>\n  <h1>Hello</h1>\n</body>\n</html>\n",
    "readme": "# Project\n\n## Overview\n\n## Usage\n",
    "notes": "# Notes\n\n- ",
    "school_essay": "Title: Untitled\n\nIntroduction\n\nBody\n\nConclusion\n",
    "homework": "# Homework\n\n- [ ] Task 1\n",
}


def _is_relative_to_path(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _is_dangerous_write_path(path: Path) -> bool:
    if os.name == "nt":
        parts = {part.lower() for part in path.parts}
        return any(part in parts for part in {"windows", "system32", "program files", "program files (x86)"})
    return any(_is_relative_to_path(path, root) for root in DANGEROUS_WRITE_ROOTS)


def safe_path_status(path: str) -> str:
    """Report whether Ares considers a write path normal or dangerous."""
    resolved = resolve_write_path(path)
    if _is_dangerous_write_path(resolved):
        return f"Blocked dangerous path: {_display_path(resolved)}"
    roots = [_display_path(root) for root in _allowed_roots()]
    return f"Safe write path: {_display_path(resolved)}\nAllowed roots include: {', '.join(roots)}; current working directory and temp directories are also supported."


def _ensure_safe_write_path(path: Path, *, confirm_dangerous: bool = False) -> str | None:
    if _is_dangerous_write_path(path) and not confirm_dangerous:
        return f"Blocked dangerous path: {_display_path(path)}. Re-run with confirm_dangerous=true only if you are absolutely sure."
    return None


def _read_text_lines(path: Path) -> tuple[list[str], bool]:
    content = path.read_text(encoding="utf-8", errors="replace")
    has_trailing_newline = content.endswith("\n")
    return content.splitlines(), has_trailing_newline


def _join_lines(lines: list[str], trailing_newline: bool = True) -> str:
    content = "\n".join(lines)
    if trailing_newline and (content or lines):
        content += "\n"
    return content


def _line_diff(path: Path, old_content: str, new_content: str) -> str:
    diff = difflib.unified_diff(
        old_content.splitlines(),
        new_content.splitlines(),
        fromfile=str(path),
        tofile=str(path),
        lineterm="",
    )
    return "\n".join(diff) or "No changes."


def show_file_with_line_numbers(path: str, start: int | None = None, end: int | None = None) -> str:
    """Show a file range with stable 1-based line numbers before editing."""
    resolved = resolve_write_path(path)
    if not resolved.exists():
        return f"File not found: {path}"
    if not resolved.is_file():
        return f"Not a file: {_display_path(resolved)}"
    lines, _ = _read_text_lines(resolved)
    total = len(lines)
    start_line = max(1, int(start or 1))
    end_line = min(total, int(end or total))
    if end_line < start_line:
        return "Invalid range: end must be greater than or equal to start."
    output = [f"[File: {_display_path(resolved)} ({total} lines total)]"]
    for line_number in range(start_line, end_line + 1):
        output.append(f"{line_number:>6}\t{lines[line_number - 1]}")
    return "\n".join(output)


def preview_diff(path: str, new_content: str) -> str:
    """Preview a unified diff for replacing a file with new_content."""
    resolved = resolve_write_path(path)
    old_content = ""
    if resolved.exists():
        if not resolved.is_file():
            return f"Not a file: {_display_path(resolved)}"
        old_content = resolved.read_text(encoding="utf-8", errors="replace")
    return _line_diff(resolved, old_content, new_content)


def backup_file(path: str, label: str = "") -> str:
    """Create a timestamped backup under .ares_backups next to the file."""
    resolved = resolve_write_path(path)
    if not resolved.exists() or not resolved.is_file():
        return f"File not found: {path}"
    backup_root = resolved.parent / ".ares_backups"
    backup_root.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    suffix = f".{label}" if label else ""
    backup_path = backup_root / f"{resolved.name}.{timestamp}{suffix}.bak"
    shutil.copy2(resolved, backup_path)
    return f"Backed up {_display_path(resolved)} → {_display_path(backup_path)}"


def _backup_path(path: Path, label: str = "") -> Path:
    backup_root = path.parent / ".ares_backups"
    backup_root.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    suffix = f".{label}" if label else ""
    return backup_root / f"{path.name}.{timestamp}{suffix}.bak"


def _write_with_backup(path: Path, new_content: str, *, dry_run: bool, confirm_dangerous: bool = False) -> str:
    blocked = _ensure_safe_write_path(path, confirm_dangerous=confirm_dangerous)
    if blocked:
        return blocked
    old_content = path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
    diff = _line_diff(path, old_content, new_content)
    if diff == "No changes.":
        return f"No changes for {_display_path(path)}"
    if dry_run:
        return f"[DRY RUN] Would update {_display_path(path)}:\n{diff}"
    if path.exists():
        shutil.copy2(path, _backup_path(path, "auto"))
    atomic_write(path, new_content)
    return f"Updated {_display_path(path)} with backup.\n{diff}"


def undo_last_edit(path: str, dry_run: bool = False) -> str:
    """Restore the newest backup for path from its .ares_backups folder."""
    resolved = resolve_write_path(path)
    backup_root = resolved.parent / ".ares_backups"
    backups = sorted(backup_root.glob(f"{resolved.name}.*.bak"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not backups:
        return f"No backup found for {_display_path(resolved)}"
    latest = backups[0]
    if dry_run:
        return f"[DRY RUN] Would restore {_display_path(latest)} → {_display_path(resolved)}"
    if resolved.exists():
        shutil.copy2(resolved, _backup_path(resolved, "before_undo"))
    shutil.copy2(latest, resolved)
    return f"Restored {_display_path(resolved)} from {_display_path(latest)}"


def insert_line(path: str, line: int, text: str, position: str = "after", dry_run: bool = False, confirm_dangerous: bool = False) -> str:
    """Insert text before or after a 1-based line number."""
    resolved = resolve_write_path(path)
    if not resolved.exists():
        return f"File not found: {path}"
    lines, trailing = _read_text_lines(resolved)
    if not 1 <= int(line) <= max(1, len(lines)):
        return f"Invalid line {line}; file has {len(lines)} line(s)."
    insert_at = int(line) - 1 if position == "before" else int(line)
    new_lines = lines[:insert_at] + text.splitlines() + lines[insert_at:]
    return _write_with_backup(resolved, _join_lines(new_lines, trailing), dry_run=dry_run, confirm_dangerous=confirm_dangerous)


def replace_lines(path: str, start: int, end: int, new_text: str, dry_run: bool = False, confirm_dangerous: bool = False) -> str:
    """Replace an inclusive 1-based line range."""
    resolved = resolve_write_path(path)
    if not resolved.exists():
        return f"File not found: {path}"
    lines, trailing = _read_text_lines(resolved)
    start_i, end_i = int(start), int(end)
    if start_i < 1 or end_i < start_i or end_i > len(lines):
        return f"Invalid range {start}-{end}; file has {len(lines)} line(s)."
    new_lines = lines[: start_i - 1] + new_text.splitlines() + lines[end_i:]
    return _write_with_backup(resolved, _join_lines(new_lines, trailing), dry_run=dry_run, confirm_dangerous=confirm_dangerous)


def delete_lines(path: str, start: int, end: int, dry_run: bool = False, confirm_dangerous: bool = False) -> str:
    """Delete an inclusive 1-based line range."""
    return replace_lines(path, start, end, "", dry_run=dry_run, confirm_dangerous=confirm_dangerous)


def append_to_file(path: str, text: str, dry_run: bool = False, confirm_dangerous: bool = False) -> str:
    resolved = resolve_write_path(path)
    old = resolved.read_text(encoding="utf-8", errors="replace") if resolved.exists() else ""
    sep = "" if not old or old.endswith("\n") else "\n"
    return _write_with_backup(resolved, old + sep + text + ("" if text.endswith("\n") else "\n"), dry_run=dry_run, confirm_dangerous=confirm_dangerous)


def prepend_to_file(path: str, text: str, dry_run: bool = False, confirm_dangerous: bool = False) -> str:
    resolved = resolve_write_path(path)
    old = resolved.read_text(encoding="utf-8", errors="replace") if resolved.exists() else ""
    prefix = text + ("" if text.endswith("\n") else "\n")
    return _write_with_backup(resolved, prefix + old, dry_run=dry_run, confirm_dangerous=confirm_dangerous)


def find_text(path: str, query: str, context: int = 2, max_results: int = 20) -> str:
    """Find text in one file and return line numbers with nearby context."""
    resolved = resolve_write_path(path)
    if not resolved.exists() or not resolved.is_file():
        return f"File not found: {path}"
    lines, _ = _read_text_lines(resolved)
    needle = query.lower()
    matches = [i for i, line in enumerate(lines, 1) if needle in line.lower()]
    if not matches:
        return f"No matches for {query!r} in {_display_path(resolved)}"
    bounded = max(1, min(int(max_results), 100))
    ctx = max(0, min(int(context), 10))
    out = [f"Found {len(matches)} match(es) in {_display_path(resolved)}:"]
    for line_no in matches[:bounded]:
        out.append(f"-- match at line {line_no} --")
        for show_no in range(max(1, line_no - ctx), min(len(lines), line_no + ctx) + 1):
            marker = ">" if show_no == line_no else " "
            out.append(f"{marker}{show_no:>6}\t{lines[show_no - 1]}")
    if len(matches) > bounded:
        out.append(f"... {len(matches) - bounded} more match(es)")
    return "\n".join(out)


def compare_files(left: str, right: str) -> str:
    left_path = resolve_write_path(left)
    right_path = resolve_write_path(right)
    if not left_path.exists() or not right_path.exists():
        return "Both files must exist to compare."
    left_content = left_path.read_text(encoding="utf-8", errors="replace")
    right_content = right_path.read_text(encoding="utf-8", errors="replace")
    diff = difflib.unified_diff(left_content.splitlines(), right_content.splitlines(), fromfile=str(left_path), tofile=str(right_path), lineterm="")
    return "\n".join(diff) or "Files are identical."


def create_file_from_template(path: str, template: str = "notes", dry_run: bool = False, confirm: bool = False, confirm_dangerous: bool = False) -> str:
    resolved = resolve_write_path(path)
    if resolved.exists() and not (confirm or dry_run):
        return f"confirm=true required to overwrite {_display_path(resolved)}"
    content = TEMPLATES.get(template.lower())
    if content is None:
        return f"Unknown template {template!r}. Available: {', '.join(sorted(TEMPLATES))}"
    return _write_with_backup(resolved, content, dry_run=dry_run, confirm_dangerous=confirm_dangerous)


def batch_file_ops(ops: list[dict], dry_run: bool = False, confirm_dangerous: bool = False, max_operations: int = 100) -> str:
    """Run line/file operations atomically, rolling back all touched files on error."""
    if not isinstance(ops, list):
        return "ops must be a list."
    if len(ops) > max(1, min(int(max_operations), 500)):
        return f"Too many operations: {len(ops)} requested."
    touched: dict[Path, str | None] = {}
    results: list[str] = []

    def remember(path: Path) -> None:
        if path not in touched:
            touched[path] = path.read_text(encoding="utf-8", errors="replace") if path.exists() else None

    try:
        for index, op in enumerate(ops, 1):
            action = str(op.get("op") or op.get("action") or "").lower()
            path_value = op.get("path") or op.get("file")
            if not path_value:
                raise ValueError(f"operation {index}: path is required")
            path_obj = resolve_write_path(str(path_value))
            blocked = _ensure_safe_write_path(path_obj, confirm_dangerous=confirm_dangerous)
            if blocked:
                raise ValueError(blocked)
            remember(path_obj)
            if action == "insert_line":
                result = insert_line(str(path_obj), int(op["line"]), str(op.get("text", "")), position=op.get("position", "after"), dry_run=dry_run, confirm_dangerous=confirm_dangerous)
            elif action == "replace_lines":
                result = replace_lines(str(path_obj), int(op["start"]), int(op["end"]), str(op.get("new_text", "")), dry_run=dry_run, confirm_dangerous=confirm_dangerous)
            elif action == "delete_lines":
                result = delete_lines(str(path_obj), int(op["start"]), int(op["end"]), dry_run=dry_run, confirm_dangerous=confirm_dangerous)
            elif action == "append":
                result = append_to_file(str(path_obj), str(op.get("text", "")), dry_run=dry_run, confirm_dangerous=confirm_dangerous)
            elif action == "prepend":
                result = prepend_to_file(str(path_obj), str(op.get("text", "")), dry_run=dry_run, confirm_dangerous=confirm_dangerous)
            else:
                raise ValueError(f"operation {index}: unsupported op {action!r}")
            if result.startswith("Invalid") or result.startswith("File not found") or result.startswith("Blocked"):
                raise ValueError(result)
            results.append(f"{index}. {result.splitlines()[0]}")
    except Exception as exc:
        if not dry_run:
            for path, old_content in touched.items():
                if old_content is None:
                    try:
                        path.unlink()
                    except FileNotFoundError:
                        pass
                else:
                    atomic_write(path, old_content)
        return f"Batch failed and rolled back: {exc}"
    status = "Dry run complete" if dry_run else "Batch applied atomically"
    return f"{status}: {len(ops)} operation(s)\n" + "\n".join(results)
