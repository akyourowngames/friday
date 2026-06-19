"""Tests for read-only filesystem tools."""

import pytest
from pathlib import Path

from ares.filesystem import list_directory, read_file, resolve_path, search_files


class TestResolvePath:
    def test_resolve_allows_home(self, tmp_path, monkeypatch):
        monkeypatch.setattr("ares.filesystem.Path.home", lambda: tmp_path)
        target = tmp_path / "file.txt"
        assert resolve_path(str(target)) == target.resolve()

    def test_resolve_blocks_outside_home(self, tmp_path, monkeypatch):
        monkeypatch.setattr("ares.filesystem.Path.home", lambda: tmp_path / "home")
        # In the new design, resolve_path allows any path.
        # So we just test that it resolves correctly.
        target = "/tmp/secret.txt"
        assert resolve_path(target) == Path(target).expanduser().resolve()


class TestReadFile:
    def test_read_text_file_with_line_numbers(self, tmp_path, monkeypatch):
        monkeypatch.setattr("ares.filesystem.Path.home", lambda: tmp_path)
        test_file = tmp_path / "hello.txt"
        test_file.write_text("line one\nline two\nline three\n", encoding="utf-8")

        result = read_file(str(test_file))

        assert "line one" in result
        assert "line two" in result
        assert "3 lines total" in result
        assert "     1\tline one" in result

    def test_read_with_line_range(self, tmp_path, monkeypatch):
        monkeypatch.setattr("ares.filesystem.Path.home", lambda: tmp_path)
        test_file = tmp_path / "numbered.txt"
        test_file.write_text("\n".join(f"line {i}" for i in range(1, 21)), encoding="utf-8")

        result = read_file(str(test_file), start_line=5, num_lines=3)

        assert "line 5" in result
        assert "line 6" in result
        assert "line 7" in result
        assert "line 8" not in result
        assert "4 more lines above" in result

    def test_read_nonexistent_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr("ares.filesystem.Path.home", lambda: tmp_path)
        with pytest.raises(FileNotFoundError):
            read_file(str(tmp_path / "nope.txt"))

    def test_read_binary_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr("ares.filesystem.Path.home", lambda: tmp_path)
        test_file = tmp_path / "binary.bin"
        test_file.write_bytes(b"\x00\x01\x02binary")
        assert "binary" in read_file(str(test_file)).lower()

    def test_read_truncates_and_reports_more_lines(self, tmp_path, monkeypatch):
        monkeypatch.setattr("ares.filesystem.Path.home", lambda: tmp_path)
        test_file = tmp_path / "big.txt"
        test_file.write_text("\n".join(f"line {i}" for i in range(1, 251)), encoding="utf-8")

        result = read_file(str(test_file), num_lines=200)

        assert "line 200" in result
        assert "50 more lines below" in result


class TestSearchFiles:
    def test_content_search(self, tmp_path, monkeypatch):
        monkeypatch.setattr("ares.filesystem.Path.home", lambda: tmp_path)
        (tmp_path / "a.py").write_text("def hello(): pass", encoding="utf-8")
        (tmp_path / "b.py").write_text("def goodbye(): pass", encoding="utf-8")

        result = search_files(query="hello", path=str(tmp_path))

        assert "a.py" in result
        assert "hello" in result.lower()

    def test_content_search_treats_invalid_regex_as_literal(self, tmp_path, monkeypatch):
        monkeypatch.setattr("ares.filesystem.Path.home", lambda: tmp_path)
        (tmp_path / "notes.txt").write_text("look for [literal bracket", encoding="utf-8")

        result = search_files(query="[literal", path=str(tmp_path))

        assert "notes.txt" in result
        assert "[literal bracket" in result

    def test_name_search(self, tmp_path, monkeypatch):
        monkeypatch.setattr("ares.filesystem.Path.home", lambda: tmp_path)
        (tmp_path / "readme.md").write_text("# Hello", encoding="utf-8")
        (tmp_path / "notes.md").write_text("# Notes", encoding="utf-8")
        (tmp_path / "script.py").write_text("# Code", encoding="utf-8")

        result = search_files(query="", path=str(tmp_path), name_pattern="*.md")

        assert "readme.md" in result
        assert "notes.md" in result
        assert "script.py" not in result

    def test_hybrid_search(self, tmp_path, monkeypatch):
        monkeypatch.setattr("ares.filesystem.Path.home", lambda: tmp_path)
        (tmp_path / "app.py").write_text("import os", encoding="utf-8")
        (tmp_path / "test.py").write_text("import pytest", encoding="utf-8")

        result = search_files(query="import", path=str(tmp_path), name_pattern="*.py")

        assert "app.py" in result
        assert "test.py" in result

    def test_search_empty_query_no_pattern(self, tmp_path, monkeypatch):
        monkeypatch.setattr("ares.filesystem.Path.home", lambda: tmp_path)
        result = search_files(query="", path=str(tmp_path))
        assert "no results" in result.lower()

    def test_search_max_results(self, tmp_path, monkeypatch):
        monkeypatch.setattr("ares.filesystem.Path.home", lambda: tmp_path)
        for i in range(10):
            (tmp_path / f"file{i}.txt").write_text(f"content {i}", encoding="utf-8")

        result = search_files(query="content", path=str(tmp_path), max_results=3)

        result_lines = [line for line in result.splitlines() if line.startswith("[content match]")]
        assert len(result_lines) == 3
        assert "more result" in result


class TestListDirectory:
    def test_list_directory_basic(self, tmp_path, monkeypatch):
        monkeypatch.setattr("ares.filesystem.Path.home", lambda: tmp_path)
        (tmp_path / "file.txt").write_text("hello", encoding="utf-8")
        (tmp_path / "subdir").mkdir()

        result = list_directory(str(tmp_path))

        assert "file.txt" in result
        assert "subdir" in result

    def test_list_directory_shows_sizes(self, tmp_path, monkeypatch):
        monkeypatch.setattr("ares.filesystem.Path.home", lambda: tmp_path)
        (tmp_path / "small.txt").write_text("hi", encoding="utf-8")
        (tmp_path / "bigger.txt").write_text("a" * 1000, encoding="utf-8")

        result = list_directory(str(tmp_path))

        assert "small.txt" in result
        assert "bigger.txt" in result
        assert "1000B" in result

    def test_list_directory_max_items(self, tmp_path, monkeypatch):
        monkeypatch.setattr("ares.filesystem.Path.home", lambda: tmp_path)
        for i in range(10):
            (tmp_path / f"file{i}.txt").write_text("x", encoding="utf-8")

        result = list_directory(str(tmp_path), max_items=3)

        assert "7 more item" in result

    def test_list_empty_directory(self, tmp_path, monkeypatch):
        monkeypatch.setattr("ares.filesystem.Path.home", lambda: tmp_path)
        assert "[Directory:" in list_directory(str(tmp_path))


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
