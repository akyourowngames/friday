"""Tests for OnboardingWizard."""


import pytest
from rich.console import Console

from ares.config import AppConfig
from ares.infra.onboarding import OnboardingWizard, _detect_os
from ares.profile import ProfileManager
from ares.soul import SoulManager


@pytest.fixture(autouse=True)
def isolate_config_path(tmp_path, monkeypatch):
    """Keep onboarding saves from touching a developer's real desktop config."""
    from ares import config as config_module

    monkeypatch.setattr(config_module, "CONFIG_PATH", tmp_path / "config.json")


class TestOnboardingWizard:
    def _make_wizard(self, tmp_path):
        sink = (tmp_path / "console.txt").open("w", encoding="utf-8")
        console = Console(file=sink)
        config = AppConfig(data_dir=str(tmp_path))
        profile_mgr = ProfileManager(data_dir=tmp_path)
        profile_mgr.ensure_exists()
        soul_mgr = SoulManager(data_dir=tmp_path)
        soul_mgr.ensure_exists()
        return OnboardingWizard(console, config, profile_mgr, soul_mgr), sink

    def test_save_writes_profile_md(self, tmp_path):
        wizard, sink = self._make_wizard(tmp_path)
        data = {
            "name": "Alice",
            "pronouns": "she/her",
            "coding_style": "Pragmatic",
            "os_terminal": "Linux / Bash",
            "assistant_style": "Concise (Jarvis-style)",
            "model": "deepseek-v4-flash-free",
            "personality": "jarvis",
            "projects": [{"name": "MyApp", "description": "A web app"}],
            "goals": ["Ship v1.0", "Learn Rust"],
        }
        wizard._save(data)
        sink.close()
        profile_content = (tmp_path / "profile.md").read_text(encoding="utf-8")
        assert "- Name: Alice" in profile_content
        assert "- Pronouns: she/her" in profile_content
        assert "- Coding style: Pragmatic" in profile_content
        assert "- MyApp — A web app" in profile_content
        assert "- Ship v1.0" in profile_content

    def test_save_writes_soul_presets(self, tmp_path):
        wizard, sink = self._make_wizard(tmp_path)
        data = {
            "name": "Bob",
            "pronouns": "",
            "coding_style": "Verbose & documented",
            "os_terminal": "macOS / zsh",
            "assistant_style": "Detailed",
            "model": "deepseek-v4-flash-free",
            "personality": "mentor",
            "projects": [],
            "goals": [],
        }
        wizard._save(data)
        sink.close()
        soul_content = (tmp_path / "soul.md").read_text(encoding="utf-8")
        assert "Educational" in soul_content
        assert "patient" in soul_content

    def test_save_writes_custom_soul_and_updates_model(self, tmp_path):
        wizard, sink = self._make_wizard(tmp_path)
        custom_soul = wizard._custom_soul("Be direct and test everything.")
        data = {
            "name": "Test",
            "pronouns": "",
            "coding_style": "Pragmatic",
            "os_terminal": "Linux",
            "assistant_style": "Concise",
            "model": "mimo-v2.5-free",
            "personality": custom_soul,
            "projects": [],
            "goals": [],
        }
        wizard._save(data)
        sink.close()
        assert wizard.config.model == "mimo-v2.5-free"
        assert wizard.config.onboarding_completed is True
        assert "Be direct and test everything." in (tmp_path / "soul.md").read_text(
            encoding="utf-8"
        )

    def test_save_empty_projects_and_goals(self, tmp_path):
        wizard, sink = self._make_wizard(tmp_path)
        data = {
            "name": "Alice",
            "pronouns": "",
            "coding_style": "Pragmatic",
            "os_terminal": "Linux",
            "assistant_style": "Concise",
            "model": "deepseek-v4-flash-free",
            "personality": "jarvis",
            "projects": [],
            "goals": [],
        }
        wizard._save(data)
        sink.close()
        profile_content = (tmp_path / "profile.md").read_text(encoding="utf-8")
        assert "## Current Projects\n\n## Goals" in profile_content

    def test_detect_os_returns_non_empty_string(self):
        assert " / " in _detect_os()
