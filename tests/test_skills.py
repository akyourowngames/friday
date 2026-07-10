from pathlib import Path

from ares.skills import SkillManager
from ares.tools.definitions import get_tool_definitions
from ares.tools.executor import ToolExecutor


class DummyStore:
    pass


def test_parse_skill_frontmatter_and_supporting_files(tmp_path):
    skill_dir = tmp_path / "coding" / "test-skill"
    refs = skill_dir / "references"
    refs.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: test-skill\ndescription: Helps test skills.\ncategory: coding\nversion: 2.0.0\nexamples:\n  - prompt: Try it.\ntest_commands:\n  - pytest\n---\n\n# Test Skill\nDo it.\n",
        encoding="utf-8",
    )
    (refs / "notes.md").write_text("extra", encoding="utf-8")

    skill = SkillManager.parse_skill_file(skill_dir / "SKILL.md")

    assert skill.name == "test-skill"
    assert skill.description == "Helps test skills."
    assert skill.category == "coding"
    assert skill.version == "2.0.0"
    assert "Do it." in skill.content
    assert [p.name for p in skill.files] == ["notes.md"]
    assert skill.examples[0]["prompt"] == "Try it."
    assert skill.test_commands == ["pytest"]
    assert skill.lint_messages == []


def test_skill_lint_reports_missing_test_commands_for_examples(tmp_path):
    skill_dir = tmp_path / "demo" / "lint-me"
    skill_dir.mkdir(parents=True)
    skill_file = skill_dir / "SKILL.md"
    skill_file.write_text(
        "---\nname: lint-me\ndescription: A useful lint demo.\ncategory: demo\nversion: 1.0.0\nexamples:\n  - prompt: Run demo.\n---\n\n# Demo\n",
        encoding="utf-8",
    )

    messages = SkillManager.lint_skill_file(skill_file)

    assert any("test_commands" in message for message in messages)


def test_skill_manager_discovery_search_crud_and_file_safety(tmp_path):
    manager = SkillManager([tmp_path])
    created = manager.create_skill(
        "My Skill",
        "---\ndescription: A reusable workflow for demos.\n---\n\n# Demo\nSteps here.",
        category="demo",
    )

    assert created.name == "my-skill"
    assert manager.get_skill("my-skill") is not None
    assert manager.search("demos")[0].name == "my-skill"
    assert manager.list_categories()["demo"] == 1

    (created.root / "references").mkdir()
    (created.root / "references" / "guide.md").write_text("guide", encoding="utf-8")
    assert manager.get_skill_file("my-skill", "references/guide.md") == "guide"

    try:
        manager.get_skill_file("my-skill", "../outside.md")
    except ValueError as exc:
        assert "inside" in str(exc)
    else:
        raise AssertionError("path traversal should be blocked")

    updated = manager.update_skill("my-skill", "---\ndescription: Updated desc.\n---\n\nUpdated")
    assert updated.description == "Updated desc."
    assert manager.delete_skill("my-skill") is True
    assert manager.get_skill("my-skill") is None


def test_relevant_skills_auto_context_and_invocation_policy(tmp_path):
    manager = SkillManager([tmp_path])
    manager.create_skill(
        "Review Diff",
        (
            "---\n"
            "description: Review git diffs and flag risky code changes. Use when the user asks for review, risk, or changed files.\n"
            "category: coding\n"
            "---\n\n"
            "# Review Diff\n"
            "Inspect the diff, list serious issues first, and verify tests.\n"
        ),
        category="coding",
    )
    manager.create_skill(
        "Deploy",
        (
            "---\n"
            "description: Deploy the application to production.\n"
            "disable-model-invocation: true\n"
            "---\n\n"
            "# Deploy\n"
            "Run release steps.\n"
        ),
        category="ops",
    )

    relevant = manager.relevant_skills("please review this diff for risk", limit=1)
    assert [skill.name for skill in relevant] == ["review-diff"]

    context = manager.auto_context("please review this diff for risk", limit=1)
    assert "## Auto-Loaded Skills" in context
    assert "# Skill: review-diff" in context
    assert "Inspect the diff" in context
    assert "deploy" not in context.lower()


def test_project_agent_skills_are_discovered_from_cwd(tmp_path, monkeypatch):
    skill_dir = tmp_path / ".agents" / "skills" / "repo-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: repo-skill\ndescription: Repo-local workflow for tests.\n---\n\n# Repo Skill\nDo repo work.",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    manager = SkillManager([])

    assert manager.get_skill("repo-skill") is not None


def test_builtin_skills_and_tool_definitions_are_available(tmp_path):
    manager = SkillManager([tmp_path])
    names = {skill.name for skill in manager.list_all()}
    assert {"code-review", "web-research", "daily-planner", "computer-use"}.issubset(names)
    assert "auto-load relevant skills silently" in manager.compact_index()

    relevant = manager.relevant_skills("Open Notepad, type a note, and save it on my desktop")
    assert "computer-use" in {skill.name for skill in relevant}

    tool_names = {tool["function"]["name"] for tool in get_tool_definitions()}
    assert {"list_skills", "load_skill", "create_skill"}.issubset(tool_names)


def test_auto_loaded_skills_require_direct_intent_signals(tmp_path):
    manager = SkillManager([tmp_path])

    desktop_files = manager.relevant_skills("show me files on desktop")
    mcp_status = manager.relevant_skills("what status of mcps")
    latest_news = manager.relevant_skills("what is the latest in news")

    assert not desktop_files
    assert not mcp_status
    assert "web-research" in {skill.name for skill in latest_news}

    computer_use = next(
        skill for skill in manager.relevant_skills("open Instagram using MCP")
        if skill.name == "computer-use"
    )
    assert manager.selection_reason(computer_use, "open Instagram using MCP") == (
        "matches a desktop action request"
    )


def test_tool_executor_skill_tools(tmp_path):
    executor = ToolExecutor(DummyStore(), DummyStore())
    executor.skill_manager = SkillManager([tmp_path])

    result = executor.execute(
        "create_skill",
        {"name": "demo-skill", "category": "demo", "content": "---\ndescription: Demo skill.\n---\n\n# Demo"},
    )
    assert "Created skill 'demo-skill'" in result
    assert "demo-skill" in executor.execute("list_skills", {"query": "demo"})
    loaded = executor.execute("load_skill", {"name": "demo-skill"})
    assert "# Skill: demo-skill" in loaded
    assert "# Demo" in loaded
