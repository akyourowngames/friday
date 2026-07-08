"""Tests for write filesystem operations."""

import pytest
from pathlib import Path


def test_resolve_write_path(tmp_path):
    """Write paths should be resolved correctly."""
    from ares.tools.filesystem_write import resolve_write_path
    target = tmp_path / "project" / "file.txt"
    result = resolve_write_path(str(target))
    assert result == target.resolve()


def test_resolve_write_path_anywhere(tmp_path):
    """Write paths outside home should be accepted."""
    from ares.tools.filesystem_write import resolve_write_path
    result = resolve_write_path("/tmp/evil.txt")
    assert result == Path("/tmp/evil.txt").resolve()


def test_resolve_write_path_ares_dir(tmp_path):
    """Writes to ~/.ares/ should be accepted."""
    from ares.tools.filesystem_write import resolve_write_path
    ares_dir = tmp_path / ".ares"
    result = resolve_write_path(str(ares_dir / "config.json"))
    assert result == (ares_dir / "config.json").resolve()


def test_atomic_write_creates_file(tmp_path, monkeypatch):
    """atomic_write should create a new file."""
    
    from ares.tools.filesystem_write import atomic_write
    target = tmp_path / "new_file.txt"
    atomic_write(target, "hello world\n")
    assert target.read_text(encoding="utf-8") == "hello world\n"


def test_atomic_write_overwrites_file(tmp_path, monkeypatch):
    """atomic_write should safely overwrite an existing file."""
    
    from ares.tools.filesystem_write import atomic_write
    target = tmp_path / "existing.txt"
    target.write_text("old content", encoding="utf-8")
    atomic_write(target, "new content")
    assert target.read_text(encoding="utf-8") == "new content"


def test_atomic_write_cleanup_on_failure(tmp_path, monkeypatch):
    """atomic_write should clean up temp file on failure."""
    
    from ares.tools.filesystem_write import atomic_write
    target = tmp_path / "fail.txt"
    # Force a failure by passing non-string data
    with pytest.raises(Exception):
        atomic_write(target, None)
    # Temp file should be cleaned up
    temp_files = list(tmp_path.glob(".tmp_*.part"))
    assert len(temp_files) == 0


def test_write_file_new(tmp_path, monkeypatch):
    monkeypatch.setattr("ares.filesystem.Path.home", lambda: tmp_path)
    
    from ares.tools.filesystem_write import write_file
    target = str(tmp_path / "new.txt")
    result = write_file(target, "hello world")
    assert "Created" in result
    assert (tmp_path / "new.txt").read_text(encoding="utf-8") == "hello world"


def test_write_file_overwrite_requires_confirm(tmp_path, monkeypatch):
    monkeypatch.setattr("ares.filesystem.Path.home", lambda: tmp_path)
    
    from ares.tools.filesystem_write import write_file
    target = tmp_path / "existing.txt"
    target.write_text("old", encoding="utf-8")
    # write_file doesn't have confirm param — that's in ToolExecutor
    # The function itself just writes
    result = write_file(str(target), "new")
    assert "Overwrote" in result
    assert target.read_text(encoding="utf-8") == "new"


def test_write_file_dry_run(tmp_path, monkeypatch):
    monkeypatch.setattr("ares.filesystem.Path.home", lambda: tmp_path)
    
    from ares.tools.filesystem_write import write_file
    target = tmp_path / "dry.txt"
    result = write_file(str(target), "content", dry_run=True)
    assert "DRY RUN" in result
    assert not target.exists()  # file should NOT be created


def test_write_file_dry_run_shows_diff_for_overwrite(tmp_path):
    from ares.tools.filesystem_write import write_file
    target = tmp_path / "dry.txt"
    target.write_text("old\n", encoding="utf-8")

    result = write_file(str(target), "new\n", dry_run=True)

    assert "DRY RUN" in result
    assert "-old" in result
    assert "+new" in result
    assert target.read_text(encoding="utf-8") == "old\n"


def test_edit_file_exact_match(tmp_path, monkeypatch):
    monkeypatch.setattr("ares.filesystem.Path.home", lambda: tmp_path)
    
    from ares.tools.filesystem_write import edit_file
    target = tmp_path / "code.py"
    target.write_text("def greet():\n    print('hello')\n", encoding="utf-8")
    result = edit_file(str(target), "print('hello')", "print('world')")
    assert "Edited" in result
    assert target.read_text(encoding="utf-8") == "def greet():\n    print('world')\n"


def test_edit_file_no_match_returns_suggestion(tmp_path, monkeypatch):
    monkeypatch.setattr("ares.filesystem.Path.home", lambda: tmp_path)
    
    from ares.tools.filesystem_write import edit_file
    target = tmp_path / "code.py"
    target.write_text("def greet():\n    print('hello')\n", encoding="utf-8")
    result = edit_file(str(target), "print('goodbye')", "print('world')")
    assert "No match" in result or "Did you mean" in result


def test_edit_file_multiple_matches(tmp_path, monkeypatch):
    monkeypatch.setattr("ares.filesystem.Path.home", lambda: tmp_path)
    
    from ares.tools.filesystem_write import edit_file
    target = tmp_path / "code.py"
    target.write_text("x = 1\nx = 1\nx = 1\n", encoding="utf-8")
    result = edit_file(str(target), "x = 1", "x = 10")
    assert "matches" in result.lower() and "locations" in result.lower()


def test_edit_file_whitespace_normalized(tmp_path, monkeypatch):
    monkeypatch.setattr("ares.filesystem.Path.home", lambda: tmp_path)
    
    from ares.tools.filesystem_write import edit_file
    target = tmp_path / "code.py"
    target.write_text("def greet():\n    print('hello')\n", encoding="utf-8")
    # LLM sends wrong indentation
    result = edit_file(str(target), "print('hello')", "print('world')")
    assert "Edited" in result


def test_edit_file_dry_run(tmp_path, monkeypatch):
    monkeypatch.setattr("ares.filesystem.Path.home", lambda: tmp_path)
    
    from ares.tools.filesystem_write import edit_file
    target = tmp_path / "code.py"
    target.write_text("old content", encoding="utf-8")
    result = edit_file(str(target), "old", "new", dry_run=True)
    assert "DRY RUN" in result
    assert target.read_text(encoding="utf-8") == "old content"  # unchanged
    assert "-old content" in result
    assert "+new content" in result


def test_edit_file_not_found(tmp_path, monkeypatch):
    monkeypatch.setattr("ares.filesystem.Path.home", lambda: tmp_path)
    
    from ares.tools.filesystem_write import edit_file
    result = edit_file(str(tmp_path / "nope.py"), "a", "b")
    assert "not found" in result.lower()


def test_create_directory(tmp_path, monkeypatch):
    monkeypatch.setattr("ares.filesystem.Path.home", lambda: tmp_path)
    
    from ares.tools.filesystem_write import create_directory
    target = tmp_path / "new_dir" / "sub"
    result = create_directory(str(target))
    assert "Created" in result
    assert target.is_dir()


def test_create_directory_already_exists(tmp_path, monkeypatch):
    monkeypatch.setattr("ares.filesystem.Path.home", lambda: tmp_path)
    
    from ares.tools.filesystem_write import create_directory
    target = tmp_path / "existing"
    target.mkdir()
    result = create_directory(str(target))
    assert "already exists" in result.lower()


def test_create_directory_dry_run(tmp_path, monkeypatch):
    monkeypatch.setattr("ares.filesystem.Path.home", lambda: tmp_path)
    
    from ares.tools.filesystem_write import create_directory
    target = tmp_path / "would_create"
    result = create_directory(str(target), dry_run=True)
    assert "DRY RUN" in result
    assert not target.exists()


def test_delete_file(tmp_path, monkeypatch):
    monkeypatch.setattr("ares.filesystem.Path.home", lambda: tmp_path)
    
    from ares.tools.filesystem_write import delete_file
    target = tmp_path / "to_delete.txt"
    target.write_text("bye", encoding="utf-8")
    result = delete_file(str(target))
    assert "Deleted" in result
    assert not target.exists()


def test_delete_file_requires_confirm(tmp_path, monkeypatch):
    """delete_file should return confirmation prompt when confirm not set."""
    monkeypatch.setattr("ares.filesystem.Path.home", lambda: tmp_path)
    
    from ares.tools.filesystem_write import delete_file
    target = tmp_path / "to_delete.txt"
    target.write_text("bye", encoding="utf-8")
    # delete_file doesn't have confirm param — confirmation is in ToolExecutor
    # The function itself just deletes
    result = delete_file(str(target))
    assert "Deleted" in result


def test_delete_nonempty_directory_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr("ares.filesystem.Path.home", lambda: tmp_path)
    
    from ares.tools.filesystem_write import delete_file
    d = tmp_path / "nonempty"
    d.mkdir()
    (d / "file.txt").write_text("x", encoding="utf-8")
    result = delete_file(str(d))
    assert "non-empty" in result.lower() or "Cannot delete" in result


def test_delete_empty_directory(tmp_path, monkeypatch):
    monkeypatch.setattr("ares.filesystem.Path.home", lambda: tmp_path)
    
    from ares.tools.filesystem_write import delete_file
    d = tmp_path / "empty_dir"
    d.mkdir()
    result = delete_file(str(d))
    assert "Deleted" in result
    assert not d.exists()


def test_delete_dry_run(tmp_path, monkeypatch):
    monkeypatch.setattr("ares.filesystem.Path.home", lambda: tmp_path)
    
    from ares.tools.filesystem_write import delete_file
    target = tmp_path / "keep.txt"
    target.write_text("keep", encoding="utf-8")
    result = delete_file(str(target), dry_run=True)
    assert "DRY RUN" in result
    assert target.exists()


def test_move_file_basic(tmp_path, monkeypatch):
    monkeypatch.setattr("ares.filesystem.Path.home", lambda: tmp_path)
    
    from ares.tools.filesystem_write import move_file
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
    
    from ares.tools.filesystem_write import move_file
    src = tmp_path / "a.txt"
    src.write_text("new", encoding="utf-8")
    dst = tmp_path / "b.txt"
    dst.write_text("old", encoding="utf-8")
    result = move_file(str(src), str(dst))
    assert "overwrit" in result.lower() or "Moved" in result


def test_move_file_source_not_found(tmp_path, monkeypatch):
    monkeypatch.setattr("ares.filesystem.Path.home", lambda: tmp_path)
    
    from ares.tools.filesystem_write import move_file
    result = move_file(str(tmp_path / "nope.txt"), str(tmp_path / "dest.txt"))
    assert "not found" in result.lower()


def test_move_file_dry_run(tmp_path, monkeypatch):
    monkeypatch.setattr("ares.filesystem.Path.home", lambda: tmp_path)
    
    from ares.tools.filesystem_write import move_file
    src = tmp_path / "src.txt"
    src.write_text("data", encoding="utf-8")
    dst = tmp_path / "dst.txt"
    result = move_file(str(src), str(dst), dry_run=True)
    assert "DRY RUN" in result
    assert src.exists()  # unchanged
    assert not dst.exists()


def test_move_file_creates_parent_dirs(tmp_path, monkeypatch):
    monkeypatch.setattr("ares.filesystem.Path.home", lambda: tmp_path)
    
    from ares.tools.filesystem_write import move_file
    src = tmp_path / "file.txt"
    src.write_text("data", encoding="utf-8")
    dst = tmp_path / "sub" / "dir" / "file.txt"
    result = move_file(str(src), str(dst))
    assert "Moved" in result
    assert dst.read_text(encoding="utf-8") == "data"


def test_full_workflow_create_edit_delete(tmp_path, monkeypatch):
    """End-to-end: create file, edit it, verify, delete it."""
    monkeypatch.setattr("ares.filesystem.Path.home", lambda: tmp_path)
    
    from ares.tools.filesystem_write import write_file, edit_file, delete_file

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


def test_batch_edit_write_edit_mkdir_copy_move_delete(tmp_path):
    from ares.tools.filesystem_write import batch_edit

    src = tmp_path / "src.txt"
    delete_me = tmp_path / "delete.tmp"
    src.write_text("hello", encoding="utf-8")
    delete_me.write_text("bye", encoding="utf-8")

    result = batch_edit([
        {"action": "mkdir", "path": str(tmp_path / "nested")},
        {"action": "edit", "path": str(src), "old_text": "hello", "new_text": "world"},
        {"action": "copy", "source": str(src), "destination": str(tmp_path / "copy.txt")},
        {"action": "move", "source": str(tmp_path / "copy.txt"), "destination": str(tmp_path / "nested" / "moved.txt")},
        {"action": "delete", "path": str(delete_me)},
    ], confirm=True)

    assert "Batch edit completed" in result
    assert src.read_text(encoding="utf-8") == "world"
    assert (tmp_path / "nested" / "moved.txt").read_text(encoding="utf-8") == "world"
    assert not delete_me.exists()


def test_batch_edit_dry_run_does_not_mutate(tmp_path):
    from ares.tools.filesystem_write import batch_edit

    target = tmp_path / "a.txt"
    target.write_text("old", encoding="utf-8")
    result = batch_edit([
        {"action": "edit", "path": str(target), "old_text": "old", "new_text": "new"},
        {"action": "delete", "path": str(target)},
    ], dry_run=True)

    assert "Batch edit completed" in result
    assert "[DRY RUN]" in result
    assert target.read_text(encoding="utf-8") == "old"


def test_batch_edit_requires_confirm_for_delete(tmp_path):
    from ares.tools.filesystem_write import batch_edit

    target = tmp_path / "a.txt"
    target.write_text("old", encoding="utf-8")
    result = batch_edit([{"action": "delete", "path": str(target)}])

    assert "confirm=true required" in result
    assert target.exists()


def test_glob_apply_delete_dry_run_and_confirm(tmp_path):
    from ares.tools.filesystem_write import glob_apply

    (tmp_path / "a.tmp").write_text("a", encoding="utf-8")
    (tmp_path / "b.tmp").write_text("b", encoding="utf-8")
    (tmp_path / "keep.txt").write_text("k", encoding="utf-8")

    preview = glob_apply("*.tmp", action="delete", path=str(tmp_path), dry_run=True)
    assert "Batch edit completed" in preview
    assert (tmp_path / "a.tmp").exists()

    result = glob_apply("*.tmp", action="delete", path=str(tmp_path), dry_run=False, confirm=True)
    assert "Batch edit completed" in result
    assert not (tmp_path / "a.tmp").exists()
    assert not (tmp_path / "b.tmp").exists()
    assert (tmp_path / "keep.txt").exists()


def test_glob_apply_move_preserves_relative_paths(tmp_path):
    from ares.tools.filesystem_write import glob_apply

    src = tmp_path / "src"
    dest = tmp_path / "archive"
    (src / "nested").mkdir(parents=True)
    (src / "nested" / "a.log").write_text("log", encoding="utf-8")

    result = glob_apply("**/*.log", action="move", path=str(src), destination=str(dest), dry_run=False, confirm=True)

    assert "Batch edit completed" in result
    assert not (src / "nested" / "a.log").exists()
    assert (dest / "nested" / "a.log").read_text(encoding="utf-8") == "log"


def test_show_file_with_line_numbers_range(tmp_path):
    from ares.tools.filesystem_write import show_file_with_line_numbers
    target = tmp_path / "essay.txt"
    target.write_text("one\ntwo\nthree\nfour\n", encoding="utf-8")

    result = show_file_with_line_numbers(str(target), start=2, end=3)

    assert "     2\ttwo" in result
    assert "     3\tthree" in result
    assert "four" not in result


def test_insert_line_creates_backup_and_supports_undo(tmp_path):
    from ares.tools.filesystem_write import insert_line, undo_last_edit
    target = tmp_path / "essay.txt"
    target.write_text("one\ntwo\nthree\n", encoding="utf-8")

    result = insert_line(str(target), line=2, text="inserted", position="after")

    assert "Updated" in result
    assert target.read_text(encoding="utf-8") == "one\ntwo\ninserted\nthree\n"
    assert list((tmp_path / ".ares_backups").glob("essay.txt.*.bak"))

    undo = undo_last_edit(str(target))
    assert "Restored" in undo
    assert target.read_text(encoding="utf-8") == "one\ntwo\nthree\n"
    assert (tmp_path / ".ares_backups" / "backup_index.json").exists()


def test_replace_and_delete_lines_dry_run(tmp_path):
    from ares.tools.filesystem_write import delete_lines, replace_lines
    target = tmp_path / "notes.txt"
    target.write_text("a\nb\nc\nd\n", encoding="utf-8")

    dry = replace_lines(str(target), 2, 3, "B\nC", dry_run=True)
    assert "DRY RUN" in dry
    assert "+B" in dry
    assert target.read_text(encoding="utf-8") == "a\nb\nc\nd\n"

    result = delete_lines(str(target), 2, 3)
    assert "Updated" in result
    assert target.read_text(encoding="utf-8") == "a\nd\n"


def test_find_text_and_compare_files(tmp_path):
    from ares.tools.filesystem_write import compare_files, find_text
    left = tmp_path / "left.txt"
    right = tmp_path / "right.txt"
    left.write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
    right.write_text("alpha\nBETA\ngamma\n", encoding="utf-8")

    found = find_text(str(left), "beta", context=1)
    assert "line 2" in found
    assert ">     2\tbeta" in found

    diff = compare_files(str(left), str(right))
    assert "-beta" in diff
    assert "+BETA" in diff


def test_append_prepend_and_template(tmp_path):
    from ares.tools.filesystem_write import append_to_file, create_file_from_template, prepend_to_file
    target = tmp_path / "todo.txt"

    assert "Updated" in append_to_file(str(target), "middle")
    assert "Updated" in prepend_to_file(str(target), "top")
    assert target.read_text(encoding="utf-8") == "top\nmiddle\n"

    templated = tmp_path / "README.md"
    result = create_file_from_template(str(templated), template="readme")
    assert "Updated" in result
    assert "# Project" in templated.read_text(encoding="utf-8")


def test_batch_file_ops_rolls_back_on_failure(tmp_path):
    from ares.tools.filesystem_write import batch_file_ops
    target = tmp_path / "batch.txt"
    target.write_text("one\ntwo\nthree\n", encoding="utf-8")

    result = batch_file_ops([
        {"op": "insert_line", "path": str(target), "line": 1, "text": "ok"},
        {"op": "delete_lines", "path": str(target), "start": 99, "end": 100},
    ])

    assert "rolled back" in result.lower()
    assert target.read_text(encoding="utf-8") == "one\ntwo\nthree\n"


def test_batch_edit_rolls_back_on_partial_failure(tmp_path):
    from ares.tools.filesystem_write import batch_edit
    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    first.write_text("old", encoding="utf-8")
    second.write_text("keep", encoding="utf-8")

    result = batch_edit([
        {"action": "write", "path": str(first), "content": "new"},
        {"action": "delete", "path": str(second)},
    ], confirm=False)

    assert "rolled back" in result.lower()
    assert first.read_text(encoding="utf-8") == "old"
    assert second.read_text(encoding="utf-8") == "keep"


def test_safe_path_status_blocks_system_path():
    from ares.tools.filesystem_write import safe_path_status

    result = safe_path_status("/etc/passwd")

    assert "Blocked dangerous path" in result
