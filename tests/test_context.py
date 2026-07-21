"""Tests for ProjectContext."""

from pathlib import Path

from ares.context.discovery import SCAN_TARGETS, ProjectContext
from ares.profile import ProfileManager
from ares.soul import SoulManager


class TestProjectContext:
    def test_discover_finds_claude_md(self, tmp_path):
        (tmp_path / "CLAUDE.md").write_text("# Project\nUse pytest.", encoding="utf-8")
        found = ProjectContext(cwd=tmp_path).discover()
        assert "CLAUDE.md" in [name for name, _content in found]

    def test_discover_finds_readme(self, tmp_path):
        (tmp_path / "README.md").write_text("# My Project\nA cool tool.", encoding="utf-8")
        found = ProjectContext(cwd=tmp_path).discover()
        assert "README.md" in [name for name, _content in found]

    def test_discover_finds_pyproject(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text('[project]\nname = "ares"', encoding="utf-8")
        found = ProjectContext(cwd=tmp_path).discover()
        assert "pyproject.toml" in [name for name, _content in found]

    def test_discover_returns_empty_if_no_files(self, tmp_path):
        assert ProjectContext(cwd=tmp_path).discover() == []

    def test_discover_respects_max_files(self, tmp_path):
        (tmp_path / "CLAUDE.md").write_text("claude", encoding="utf-8")
        (tmp_path / "AGENTS.md").write_text("agents", encoding="utf-8")
        (tmp_path / "README.md").write_text("readme", encoding="utf-8")

        found = ProjectContext(cwd=tmp_path).discover(max_files=2)

        assert len(found) == 2
        assert [name for name, _content in found] == ["CLAUDE.md", "AGENTS.md"]

    def test_discover_truncates_large_files(self, tmp_path):
        big = "\n".join(f"line {i}" for i in range(300))
        (tmp_path / "CLAUDE.md").write_text(big, encoding="utf-8")

        found = ProjectContext(cwd=tmp_path).discover()

        assert "more lines" in found[0][1]

    def test_discover_skips_binary_files(self, tmp_path):
        (tmp_path / "README.md").write_bytes(b"\x00\x01\x02\x03")
        assert ProjectContext(cwd=tmp_path).discover() == []

    def test_get_context_wraps_content(self, tmp_path):
        (tmp_path / "README.md").write_text("# My Project", encoding="utf-8")
        result = ProjectContext(cwd=tmp_path).get_context()
        assert "Current Project Context" in result
        assert "My Project" in result

    def test_get_context_empty_if_no_files(self, tmp_path):
        assert ProjectContext(cwd=tmp_path).get_context() == ""

    def test_get_context_respects_token_budget(self, tmp_path):
        (tmp_path / "README.md").write_text("word " * 500, encoding="utf-8")
        result = ProjectContext(cwd=tmp_path).get_context(token_budget=50)
        assert len(result.split()) < 100
        assert "truncated" in result.lower()

    def test_disabled_context_discovers_nothing(self, tmp_path):
        (tmp_path / "README.md").write_text("# My Project", encoding="utf-8")
        assert ProjectContext(cwd=tmp_path, enabled=False).discover() == []

    def test_scan_targets_are_valid(self):
        assert SCAN_TARGETS
        for name, max_lines in SCAN_TARGETS:
            assert isinstance(name, str)
            assert isinstance(max_lines, int)
            assert max_lines > 0


def test_static_context_file_caches_reuse_reads_and_refresh_after_edits(tmp_path, monkeypatch):
    soul = SoulManager(tmp_path)
    soul.ensure_exists()
    soul.soul_path.write_text("# Soul\nOriginal", encoding="utf-8")
    profile = ProfileManager(tmp_path)
    profile.ensure_exists()
    imported = tmp_path / "preferences.md"
    imported.write_text("Original preference", encoding="utf-8")
    profile.profile_path.write_text("## Preferences\n@preferences.md", encoding="utf-8")
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    readme = project_dir / "README.md"
    readme.write_text("# Original project", encoding="utf-8")
    project = ProjectContext(cwd=project_dir)

    original_read_text = Path.read_text
    original_read_bytes = Path.read_bytes
    text_reads: list[Path] = []
    byte_reads: list[Path] = []

    def spy_read_text(path, *args, **kwargs):
        resolved = path.resolve()
        if resolved in {soul.soul_path.resolve(), profile.profile_path.resolve(), imported.resolve()}:
            text_reads.append(resolved)
        return original_read_text(path, *args, **kwargs)

    def spy_read_bytes(path, *args, **kwargs):
        if path.resolve() == readme.resolve():
            byte_reads.append(path.resolve())
        return original_read_bytes(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", spy_read_text)
    monkeypatch.setattr(Path, "read_bytes", spy_read_bytes)

    assert "Original" in soul.read()
    assert "Original" in soul.read()
    assert "Original preference" in profile.get_context()
    assert "Original preference" in profile.get_context()
    assert "Original project" in project.get_context()
    assert "Original project" in project.get_context()
    assert text_reads.count(soul.soul_path.resolve()) == 1
    assert text_reads.count(profile.profile_path.resolve()) == 1
    assert text_reads.count(imported.resolve()) == 1
    assert byte_reads.count(readme.resolve()) == 1

    # Different sizes make the invalidation deterministic even on filesystems
    # with a coarse timestamp resolution; caches also include mtime nanoseconds.
    soul.soul_path.write_text("# Soul\nUpdated and longer", encoding="utf-8")
    imported.write_text("Updated preference with extra detail", encoding="utf-8")
    readme.write_text("# Updated project with extra detail", encoding="utf-8")
    assert "Updated and longer" in soul.read()
    assert "Updated preference" in profile.get_context()
    assert "Updated project" in project.get_context()
    assert text_reads.count(soul.soul_path.resolve()) == 2
    assert text_reads.count(imported.resolve()) == 2
    assert byte_reads.count(readme.resolve()) == 2
