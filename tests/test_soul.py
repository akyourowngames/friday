"""Tests for SoulManager."""

from ares.soul import SOUL_TEMPLATE, SoulManager


class TestSoulManager:
    def test_ensure_exists_creates_file(self, tmp_path):
        manager = SoulManager(data_dir=tmp_path)
        manager.ensure_exists()
        assert (tmp_path / "soul.md").exists()

    def test_ensure_exists_does_not_overwrite(self, tmp_path):
        path = tmp_path / "soul.md"
        path.write_text("custom soul", encoding="utf-8")
        manager = SoulManager(data_dir=tmp_path)
        manager.ensure_exists()
        assert path.read_text(encoding="utf-8") == "custom soul"

    def test_read_returns_content(self, tmp_path):
        (tmp_path / "soul.md").write_text("Be concise.", encoding="utf-8")
        assert SoulManager(data_dir=tmp_path).read() == "Be concise."

    def test_read_returns_empty_if_missing(self, tmp_path):
        assert SoulManager(data_dir=tmp_path).read() == ""

    def test_get_context_wraps_content(self, tmp_path):
        (tmp_path / "soul.md").write_text("Personality rules.", encoding="utf-8")
        context = SoulManager(data_dir=tmp_path).get_context()
        assert "Ares Personality" in context
        assert "Personality rules." in context

    def test_get_context_respects_token_budget(self, tmp_path):
        (tmp_path / "soul.md").write_text("word " * 500, encoding="utf-8")
        context = SoulManager(data_dir=tmp_path).get_context(token_budget=50)
        assert len(context.split()) < 100
        assert "truncated" in context.lower()

    def test_custom_path(self, tmp_path):
        custom = tmp_path / "custom-soul.md"
        manager = SoulManager(data_dir=tmp_path / "data", soul_path=custom)
        manager.ensure_exists()
        assert custom.exists()

    def test_template_is_valid_markdown(self):
        assert "# Ares" in SOUL_TEMPLATE
        assert "## Personality" in SOUL_TEMPLATE
