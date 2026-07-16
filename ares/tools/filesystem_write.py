"""Write file system operations for Ares."""

from __future__ import annotations

import difflib
import json
import os
import shutil
import tempfile
import stat
from dataclasses import dataclass, field
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path

from ares.tools.filesystem import _allowed_roots, _display_path, _format_size, _normalize_path, SKIP_DIRS


@dataclass
class OperationResult:
    """Internal outcome used by batch_edit, never inferred from happy text."""
    ok: bool
    changed: bool
    error: str = ""
    paths: tuple[Path, ...] = field(default_factory=tuple)
    message: str = ""


@dataclass
class _TreeSnapshot:
    exists: bool
    kind: str = ""
    mode: int | None = None
    atime_ns: int | None = None
    mtime_ns: int | None = None
    data: bytes | None = None
    link_target: str | None = None
    children: dict[str, "_TreeSnapshot"] = field(default_factory=dict)


def resolve_write_path(path: str) -> Path:
    """Resolve a write path. No access restrictions."""
    normalized = _normalize_path(path)
    expanded = Path(normalized).expanduser().resolve()
    return expanded


def atomic_write(path: Path, data: str, encoding: str = "utf-8") -> None:
    """Atomically write data to a file using temp-file-then-rename."""
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    prior = None
    with suppress(OSError):
        prior = path.stat()

    fd, tmp = tempfile.mkstemp(dir=str(parent), prefix=".tmp_", suffix=".part")
    try:
        # newline="" preserves caller-provided CRLF/LF bytes instead of
        # applying the host platform's newline translation.
        with os.fdopen(fd, "w", encoding=encoding, newline="") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, str(path))
        if prior is not None:
            with suppress(OSError):
                os.chmod(path, stat.S_IMODE(prior.st_mode))
            with suppress(OSError):
                os.utime(path, ns=(prior.st_atime_ns, prior.st_mtime_ns))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_file(path: str, content: str, dry_run: bool = False, confirm: bool = False) -> str:
    """Create or overwrite a file."""
    resolved = resolve_write_path(path)
    exists = resolved.exists()
    byte_count = len(str(content).encode("utf-8"))

    preview = (
        f"{'Overwrite' if exists else 'Create'} {_display_path(resolved)} "
        f"({byte_count:,} bytes)"
    )
    if dry_run:
        if exists and resolved.is_file():
            old_content = resolved.read_text(encoding="utf-8", errors="replace")
            return f"[DRY RUN] {preview}:\n{_line_diff(resolved, old_content, content)}"
        return f"[DRY RUN] {preview}"

    if exists and not confirm:
        return (
            f"⚠ CONFIRM REQUIRED: This will overwrite {_display_path(resolved)}. "
            "Re-call with confirm=true to proceed."
        )

    atomic_write(resolved, content)
    action = "Overwrote" if exists else "Created"
    return f"{action} {_display_path(resolved)} ({byte_count:,} bytes; changed=true)"


def _read_text_preserving_newlines(path: Path) -> str:
    with path.open("r", encoding="utf-8", errors="replace", newline="") as handle:
        return handle.read()


def _preferred_newline(content: str) -> str:
    return "\r\n" if "\r\n" in content else "\n"


def _with_file_newlines(text: str, content: str) -> str:
    newline = _preferred_newline(content)
    normalized = str(text).replace("\r\n", "\n").replace("\r", "\n")
    return normalized.replace("\n", newline)


def edit_file(path: str, old_text: str, new_text: str, dry_run: bool = False) -> str:
    """Edit a file by searching and replacing text with resilience cascade."""
    resolved = resolve_write_path(path)
    if not resolved.exists():
        return f"File not found: {path}"

    try:
        content = _read_text_preserving_newlines(resolved)
    except PermissionError:
        return f"Permission denied: {_display_path(resolved)}"

    # 1. Exact match
    count = content.count(old_text)
    if count == 1:
        new_content = content.replace(old_text, _with_file_newlines(new_text, content), 1)
        diff = _line_diff(resolved, content, new_content)
        if dry_run:
            return f"[DRY RUN] Would edit {_display_path(resolved)} ({len(old_text)} → {len(new_text)} chars):\n{diff}"
        _create_backup(resolved, "edit")
        atomic_write(resolved, new_content)
        return f"Edited {_display_path(resolved)} (replaced {len(old_text)} chars; changed=true)\n{diff}"

    if count > 1:
        line_numbers = _matching_line_numbers(content, old_text)
        hint = f" Matching lines: {', '.join(map(str, line_numbers[:10]))}." if line_numbers else ""
        return (
            f"old_text matches {count} locations in {_display_path(resolved)}. "
            "Provide more context to make it unique."
            f"{hint}"
        )

    # 2. Whitespace-normalized match
    old_lines = old_text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if old_lines and old_lines[-1] == "":
        old_lines.pop()
    content_lines = content.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    trailing_newline = bool(content_lines and content_lines[-1] == "")
    if trailing_newline:
        content_lines.pop()
    match_idx = _find_whitespace_match(old_lines, content_lines)
    if match_idx is not None:
        replacement_lines = _with_file_newlines(new_text, "\n").split("\n")
        if replacement_lines and replacement_lines[-1] == "":
            replacement_lines.pop()
        new_content_lines = content_lines[:match_idx] + replacement_lines + content_lines[match_idx + len(old_lines):]
        newline = _preferred_newline(content)
        new_content = newline.join(new_content_lines)
        if trailing_newline:
            new_content += newline
        diff = _line_diff(resolved, content, new_content)
        if dry_run:
            return f"[DRY RUN] Would edit {_display_path(resolved)} (whitespace-normalized match):\n{diff}"
        _create_backup(resolved, "edit")
        atomic_write(resolved, new_content)
        return f"Edited {_display_path(resolved)} (whitespace-normalized match; changed=true)\n{diff}"

    # 3. "Did you mean?" suggestion
    suggestion = _find_closest_match(old_text, content)
    if suggestion:
        return (
            f"No match found for old_text in {_display_path(resolved)}.\n"
            f"Did you mean:\n---\n{suggestion}\n---"
        )

    return f"No match found for old_text in {_display_path(resolved)}."


def _matching_line_numbers(content: str, needle: str) -> list[int]:
    """Return 1-based line numbers whose text contains the needle."""
    return [
        index
        for index, line in enumerate(content.splitlines(), 1)
        if needle in line
    ]


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
    """Execute edits transactionally, including directory trees and backups."""
    if not isinstance(operations, list):
        return "operations must be a list."
    bounded = max(1, min(int(max_operations), 500))
    if len(operations) > bounded:
        return f"Too many operations: {len(operations)} requested, max is {bounded}."

    def exists_lexically(path: Path) -> bool:
        return path.exists() or path.is_symlink()

    def snapshot(path: Path) -> _TreeSnapshot:
        try:
            info = path.lstat()
        except FileNotFoundError:
            return _TreeSnapshot(exists=False)
        node = _TreeSnapshot(
            exists=True,
            mode=stat.S_IMODE(info.st_mode),
            atime_ns=info.st_atime_ns,
            mtime_ns=info.st_mtime_ns,
        )
        if path.is_symlink():
            node.kind = "symlink"
            node.link_target = os.readlink(path)
        elif stat.S_ISDIR(info.st_mode):
            node.kind = "directory"
            for child in path.iterdir():
                node.children[child.name] = snapshot(child)
        else:
            node.kind = "file"
            node.data = path.read_bytes()
        return node

    def remove(path: Path) -> None:
        if path.is_symlink() or path.is_file():
            path.unlink()
        elif path.is_dir():
            shutil.rmtree(path)

    def restore(path: Path, node: _TreeSnapshot) -> None:
        if exists_lexically(path):
            remove(path)
        if not node.exists:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        if node.kind == "symlink":
            os.symlink(node.link_target or "", path, target_is_directory=False)
            return
        if node.kind == "file":
            path.write_bytes(node.data or b"")
        elif node.kind == "directory":
            path.mkdir(parents=True, exist_ok=True)
            for name, child in node.children.items():
                restore(path / name, child)
        if node.mode is not None:
            with suppress(OSError):
                os.chmod(path, node.mode)
        if node.atime_ns is not None and node.mtime_ns is not None:
            with suppress(OSError):
                os.utime(path, ns=(node.atime_ns, node.mtime_ns))

    def action_paths(operation: dict) -> list[Path]:
        action = str(operation.get("action", "")).casefold().strip()
        if action in {"write", "edit", "delete", "mkdir", "create_directory"} and "path" in operation:
            return [resolve_write_path(operation["path"])]
        if action in {"move", "copy"} and "source" in operation and "destination" in operation:
            return [resolve_write_path(operation["source"]), resolve_write_path(operation["destination"])]
        return []

    roots: list[Path] = []
    created_parents: set[Path] = set()
    for operation in operations:
        if not isinstance(operation, dict):
            continue
        for target in action_paths(operation):
            roots.append(target)
            roots.append(target.parent / ".ares_backups")
            parent = target.parent
            while not exists_lexically(parent):
                created_parents.add(parent)
                if parent == parent.parent:
                    break
                parent = parent.parent
    # A parent snapshot already contains any child snapshot, and retaining only
    # roots makes restore order unambiguous for moves of full directories.
    selected_roots: list[Path] = []
    for candidate in sorted(set(roots), key=lambda item: (len(item.parts), str(item))):
        if not any(candidate == root or root in candidate.parents for root in selected_roots):
            selected_roots.append(candidate)
    snapshots = {root: snapshot(root) for root in selected_roots} if not dry_run else {}

    def outcome(message: str, *, paths: list[Path], changed: bool, dry: bool) -> OperationResult:
        lowered = message.casefold()
        errors = (
            "error:", "no match found", "matches ", "file not found", "source not found",
            "cannot delete", "not a file", "not a directory", "permission denied", "confirm required",
            "unsupported action", "operations must",
        )
        failed = any(marker in lowered for marker in errors)
        return OperationResult(not failed, changed and not dry, message if failed else "", tuple(paths), message)

    results: list[str] = []
    try:
        for index, operation in enumerate(operations, 1):
            if not isinstance(operation, dict):
                raise ValueError(f"operation {index}: operation must be an object")
            action = str(operation.get("action", "")).casefold().strip()
            op_dry_run = bool(operation.get("dry_run", dry_run))
            paths = action_paths(operation)
            if action == "write":
                target = paths[0]
                if target.exists() and not confirm and not op_dry_run:
                    result = OperationResult(False, False, f"confirm=true required to overwrite {_display_path(target)}", tuple(paths))
                else:
                    message = write_file(operation["path"], operation.get("content", ""), dry_run=op_dry_run, confirm=True)
                    result = outcome(message, paths=paths, changed=True, dry=op_dry_run)
            elif action == "edit":
                message = edit_file(operation["path"], operation["old_text"], operation["new_text"], dry_run=op_dry_run)
                result = outcome(message, paths=paths, changed=True, dry=op_dry_run)
            elif action == "delete":
                if not confirm and not op_dry_run:
                    result = OperationResult(False, False, "confirm=true required for delete", tuple(paths))
                else:
                    message = delete_file(operation["path"], dry_run=op_dry_run)
                    result = outcome(message, paths=paths, changed=True, dry=op_dry_run)
            elif action == "move":
                source, destination = paths
                if destination.exists() and not confirm and not op_dry_run:
                    result = OperationResult(False, False, f"confirm=true required to overwrite {_display_path(destination)}", tuple(paths))
                else:
                    message = move_file(operation["source"], operation["destination"], dry_run=op_dry_run)
                    result = outcome(message, paths=paths, changed=True, dry=op_dry_run)
            elif action == "copy":
                source, destination = paths
                if not source.exists():
                    result = OperationResult(False, False, f"Source not found: {operation['source']}", tuple(paths))
                elif destination.exists() and not confirm and not op_dry_run:
                    result = OperationResult(False, False, f"confirm=true required to overwrite {_display_path(destination)}", tuple(paths))
                elif op_dry_run:
                    result = OperationResult(True, False, paths=tuple(paths), message=f"[DRY RUN] Would copy {_display_path(source)} → {_display_path(destination)}")
                else:
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    if source.is_dir():
                        shutil.copytree(source, destination, dirs_exist_ok=confirm)
                    else:
                        shutil.copy2(source, destination)
                    result = OperationResult(True, True, paths=tuple(paths), message=f"Copy {_display_path(source)} → {_display_path(destination)}")
            elif action in {"mkdir", "create_directory"}:
                message = create_directory(operation["path"], dry_run=op_dry_run)
                result = outcome(message, paths=paths, changed="already exists" not in message.casefold(), dry=op_dry_run)
            else:
                result = OperationResult(False, False, f"Unsupported action: {action or '<missing>'}", tuple(paths))
            if not result.ok:
                raise ValueError(result.error or result.message)
            results.append(f"{index}. {result.message}")
    except Exception as exc:
        rollback_errors: list[str] = []
        if not dry_run:
            # Remove/recreate whole snapshot roots in a deterministic order so
            # a moved directory and an overwritten destination both return
            # byte-for-byte to their original state.
            for root in sorted(snapshots, key=lambda item: len(item.parts), reverse=True):
                try:
                    restore(root, snapshots[root])
                except Exception as restore_exc:
                    rollback_errors.append(f"{root}: {restore_exc}")
            for parent in sorted(created_parents, key=lambda item: len(item.parts), reverse=True):
                with suppress(OSError):
                    parent.rmdir()
        suffix = "" if not rollback_errors else f"\nRollback errors: {'; '.join(rollback_errors)}"
        return (
            f"Batch edit failed and rolled back: {exc}{suffix}\n"
            + ("\n".join(results) if results else "No operations completed.")
        )
    return f"Batch edit completed: {len(operations)} operation(s)\n" + "\n".join(results)


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
        unix_root_aliases = {"bin", "boot", "dev", "etc", "lib", "lib64", "proc", "root", "sbin", "sys", "usr", "var"}
        root_child = path.parts[1].lower() if len(path.parts) > 1 else ""
        return any(part in parts for part in {"windows", "system32", "program files", "program files (x86)"}) or root_child in unix_root_aliases
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


def _read_text_lines(path: Path) -> tuple[list[str], bool, str]:
    content = path.read_text(encoding="utf-8", errors="replace")
    has_trailing_newline = content.endswith("\n")
    newline = "\r\n" if "\r\n" in content and content.count("\r\n") >= content.count("\n") / 2 else "\n"
    return content.splitlines(), has_trailing_newline, newline


def _join_lines(lines: list[str], trailing_newline: bool = True, newline: str = "\n") -> str:
    content = newline.join(lines)
    if trailing_newline and (content or lines):
        content += newline
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
    lines, _, _newline = _read_text_lines(resolved)
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
    safe_label = _safe_label(label)
    backup_path = _create_backup(resolved, safe_label or "manual")
    return f"Backed up {_display_path(resolved)} → {_display_path(backup_path)}\nRestore point: {safe_label or backup_path.name}"


def _backup_path(path: Path, label: str = "") -> Path:
    backup_root = path.parent / ".ares_backups"
    backup_root.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    safe_label = _safe_label(label)
    suffix = f".{safe_label}" if safe_label else ""
    backup_path = backup_root / f"{path.name}.{timestamp}{suffix}.bak"
    return backup_path


def _safe_label(label: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in (label or "").strip())[:80]


def _record_backup(source: Path, backup_path: Path, *, label: str) -> None:
    index_path = backup_path.parent / "backup_index.json"
    try:
        existing = json.loads(index_path.read_text(encoding="utf-8")) if index_path.exists() else []
        if not isinstance(existing, list):
            existing = []
    except (OSError, json.JSONDecodeError):
        existing = []
    existing.append({
        "source": str(source),
        "backup": str(backup_path),
        "label": label,
        "created_at": datetime.now(timezone.utc).isoformat(),
    })
    # The backup file is already fsynced before this index is changed.  An
    # index entry can therefore never point at a backup that was not created.
    atomic_write(index_path, json.dumps(existing, indent=2) + "\n")


def _create_backup(source: Path, label: str = "auto") -> Path:
    """Copy/fsync a backup first, then atomically publish its index record."""
    backup_path = _backup_path(source, label)
    try:
        shutil.copy2(source, backup_path)
        with backup_path.open("r+b") as handle:
            os.fsync(handle.fileno())
        _record_backup(source, backup_path, label=_safe_label(label) or "auto")
        return backup_path
    except BaseException:
        with suppress(FileNotFoundError):
            backup_path.unlink()
        raise


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
        _create_backup(path, "auto")
    atomic_write(path, new_content)
    return f"Updated {_display_path(path)} with backup.\n{diff}"


def undo_last_edit(path: str, dry_run: bool = False) -> str:
    """Restore the newest backup for path from its .ares_backups folder."""
    resolved = resolve_write_path(path)
    backup_root = resolved.parent / ".ares_backups"
    # copy2 preserves the source mtime, so filesystem mtimes cannot identify
    # the newest restore point. The UTC microsecond timestamp is embedded in
    # every backup filename and sorts chronologically.
    backups = sorted(backup_root.glob(f"{resolved.name}.*.bak"), key=lambda p: p.name, reverse=True)
    if not backups:
        return f"No backup found for {_display_path(resolved)}"
    latest = backups[0]
    if dry_run:
        diff = ""
        if resolved.exists():
            diff = "\n" + _line_diff(resolved, resolved.read_text(encoding="utf-8", errors="replace"), latest.read_text(encoding="utf-8", errors="replace"))
        return f"[DRY RUN] Would restore {_display_path(latest)} → {_display_path(resolved)}{diff}"
    if resolved.exists():
        _create_backup(resolved, "before_undo")
    before = resolved.read_text(encoding="utf-8", errors="replace") if resolved.exists() else ""
    after = latest.read_text(encoding="utf-8", errors="replace")
    shutil.copy2(latest, resolved)
    return f"Restored {_display_path(resolved)} from {_display_path(latest)}\n{_line_diff(resolved, before, after)}"


def insert_line(path: str, line: int, text: str, position: str = "after", dry_run: bool = False, confirm_dangerous: bool = False) -> str:
    """Insert text before or after a 1-based line number."""
    resolved = resolve_write_path(path)
    if not resolved.exists():
        return f"File not found: {path}"
    lines, trailing, newline = _read_text_lines(resolved)
    if not 1 <= int(line) <= max(1, len(lines)):
        return f"Invalid line {line}; file has {len(lines)} line(s)."
    insert_at = int(line) - 1 if position == "before" else int(line)
    new_lines = lines[:insert_at] + text.splitlines() + lines[insert_at:]
    return _write_with_backup(resolved, _join_lines(new_lines, trailing, newline), dry_run=dry_run, confirm_dangerous=confirm_dangerous)


def replace_lines(path: str, start: int, end: int, new_text: str, dry_run: bool = False, confirm_dangerous: bool = False) -> str:
    """Replace an inclusive 1-based line range."""
    resolved = resolve_write_path(path)
    if not resolved.exists():
        return f"File not found: {path}"
    lines, trailing, newline = _read_text_lines(resolved)
    start_i, end_i = int(start), int(end)
    if start_i < 1 or end_i < start_i or end_i > len(lines):
        return f"Invalid range {start}-{end}; file has {len(lines)} line(s)."
    new_lines = lines[: start_i - 1] + new_text.splitlines() + lines[end_i:]
    return _write_with_backup(resolved, _join_lines(new_lines, trailing, newline), dry_run=dry_run, confirm_dangerous=confirm_dangerous)


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
    lines, _, _newline = _read_text_lines(resolved)
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
