"""Tests for ProjectContext."""

from ares.context import SCAN_TARGETS, ProjectContext


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
