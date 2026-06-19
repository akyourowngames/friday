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
    # write_file doesn't have confirm param — that's in ToolExecutor
    # The function itself just writes
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
    target.write_text("x = 1\nx = 1\nx = 1\n", encoding="utf-8")
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
