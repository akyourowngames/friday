"""Tests for ProfileManager."""

from ares.profile import PROFILE_TEMPLATE, ProfileManager


class TestProfileManager:
    def test_ensure_exists_creates_file(self, tmp_path):
        manager = ProfileManager(data_dir=tmp_path)
        manager.ensure_exists()
        assert (tmp_path / "profile.md").exists()

    def test_ensure_exists_does_not_overwrite(self, tmp_path):
        path = tmp_path / "profile.md"
        path.write_text("my profile", encoding="utf-8")
        manager = ProfileManager(data_dir=tmp_path)
        manager.ensure_exists()
        assert path.read_text(encoding="utf-8") == "my profile"

    def test_read_returns_content(self, tmp_path):
        (tmp_path / "profile.md").write_text("# About Me\nName: Alice", encoding="utf-8")
        assert "Alice" in ProfileManager(data_dir=tmp_path).read()

    def test_read_returns_empty_if_missing(self, tmp_path):
        assert ProfileManager(data_dir=tmp_path).read() == ""

    def test_resolve_imports_inlines_file(self, tmp_path):
        ref_file = tmp_path / "about.md"
        ref_file.write_text("I like Python.", encoding="utf-8")
        manager = ProfileManager(data_dir=tmp_path)

        result = manager.resolve_imports(f"# About Me\n@{ref_file}")

        assert "I like Python." in result
        assert f"@{ref_file}" not in result

    def test_resolve_imports_supports_relative_paths(self, tmp_path):
        ref_file = tmp_path / "notes.md"
        ref_file.write_text("Relative note.", encoding="utf-8")
        manager = ProfileManager(data_dir=tmp_path)

        result = manager.resolve_imports("# About Me\n@notes.md")

        assert "Relative note." in result

    def test_resolve_imports_handles_missing_file(self, tmp_path):
        manager = ProfileManager(data_dir=tmp_path)
        result = manager.resolve_imports("# About Me\n@missing.md")
        assert "file not found" in result.lower()

    def test_get_context_wraps_content(self, tmp_path):
        (tmp_path / "profile.md").write_text("Name: Bob", encoding="utf-8")
        context = ProfileManager(data_dir=tmp_path).get_context()
        assert "User Profile" in context
        assert "Bob" in context

    def test_get_context_empty_if_missing(self, tmp_path):
        assert ProfileManager(data_dir=tmp_path).get_context() == ""

    def test_get_context_resolves_imports(self, tmp_path):
        ref = tmp_path / "notes.md"
        ref.write_text("My notes here.", encoding="utf-8")
        (tmp_path / "profile.md").write_text(f"# Me\n@{ref}", encoding="utf-8")
        context = ProfileManager(data_dir=tmp_path).get_context()
        assert "My notes here." in context

    def test_get_context_respects_token_budget(self, tmp_path):
        (tmp_path / "profile.md").write_text("word " * 500, encoding="utf-8")
        context = ProfileManager(data_dir=tmp_path).get_context(token_budget=50)
        assert len(context.split()) < 100
        assert "truncated" in context.lower()

    def test_custom_path(self, tmp_path):
        custom = tmp_path / "custom-profile.md"
        manager = ProfileManager(data_dir=tmp_path / "data", profile_path=custom)
        manager.ensure_exists()
        assert custom.exists()

    def test_template_is_valid_markdown(self):
        assert "# About Me" in PROFILE_TEMPLATE
        assert "## Preferences" in PROFILE_TEMPLATE

    def test_apply_updates_preserves_custom_sections(self, tmp_path):
        manager = ProfileManager(data_dir=tmp_path)
        manager.write(
            "# About Me\n\n## Preferences\n- Theme: Light\n\n"
            "## Custom Research\nKeep this prose exactly.\n"
        )

        applied = manager.apply_updates([
            {"section": "Preferences", "key": "Theme", "value": "Dark"},
            {"section": "Notes", "key": "Timezone", "value": "Asia/Calcutta"},
        ])

        content = manager.read()
        assert len(applied) == 2
        assert "- Theme: Dark" in content
        assert "- Timezone: Asia/Calcutta" in content
        assert "Keep this prose exactly." in content

    def test_is_populated_returns_false_for_empty_template(self, tmp_path):
        manager = ProfileManager(data_dir=tmp_path)
        manager.ensure_exists()
        assert manager.is_populated() is False

    def test_is_populated_returns_true_when_name_set(self, tmp_path):
        path = tmp_path / "profile.md"
        path.write_text(
            "# About Me\n\n## Identity\n- Name: Alice\n- Pronouns: she/her\n",
            encoding="utf-8",
        )
        assert ProfileManager(data_dir=tmp_path).is_populated() is True

    def test_is_populated_returns_false_for_missing_file(self, tmp_path):
        assert ProfileManager(data_dir=tmp_path).is_populated() is False

    def test_is_populated_returns_false_for_empty_name(self, tmp_path):
        path = tmp_path / "profile.md"
        path.write_text(
            "# About Me\n\n## Identity\n- Name: \n- Pronouns: \n",
            encoding="utf-8",
        )
        assert ProfileManager(data_dir=tmp_path).is_populated() is False

    def test_is_populated_returns_true_when_only_name(self, tmp_path):
        path = tmp_path / "profile.md"
        path.write_text(
            "# About Me\n\n## Identity\n- Name: Bob\n",
            encoding="utf-8",
        )
        assert ProfileManager(data_dir=tmp_path).is_populated() is True
