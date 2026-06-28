"""Integration tests for the proactive context pipeline."""

from ares.context import ProjectContext
from ares.context_blend import build_context_prompt
from ares.profile import ProfileManager
from ares.soul import SoulManager


def test_full_context_pipeline(tmp_path):
    soul_manager = SoulManager(data_dir=tmp_path)
    soul_manager.ensure_exists()
    soul_manager.soul_path.write_text(
        "## Personality\n- Be concise.\n- Be helpful.",
        encoding="utf-8",
    )

    profile_manager = ProfileManager(data_dir=tmp_path)
    profile_manager.ensure_exists()
    profile_manager.profile_path.write_text(
        "## Identity\n- Name: Alice\n\n## Preferences\n- Coding: Python",
        encoding="utf-8",
    )

    project_dir = tmp_path / "project"
    project_dir.mkdir()
    (project_dir / "README.md").write_text("# My Project\nA Python tool.", encoding="utf-8")
    project_context = ProjectContext(cwd=project_dir)

    context = build_context_prompt(
        soul_context=soul_manager.get_context(),
        profile_context=profile_manager.get_context(),
        project_context=project_context.get_context(),
        memories=[
            {"fact_id": 1, "fact_text": "Alice likes coffee", "category": "preference", "importance": 0.8},
            {"fact_id": 2, "fact_text": "Works on Ares project", "category": "project", "importance": 0.6},
        ],
    )

    assert "Alice" in context
    assert "coffee" in context
    assert "My Project" in context
    assert "Personality" in context
    assert context.index("Personality") < context.index("Alice") < context.index("My Project")


def test_context_without_project(tmp_path):
    soul_manager = SoulManager(data_dir=tmp_path)
    soul_manager.ensure_exists()
    profile_manager = ProfileManager(data_dir=tmp_path)
    profile_manager.ensure_exists()

    context = build_context_prompt(
        soul_context=soul_manager.get_context(),
        profile_context=profile_manager.get_context(),
    )

    assert "Ares Personality" in context
    assert "User Profile" in context
