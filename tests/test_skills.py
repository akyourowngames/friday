
import pytest

from ares.skills.discovery import SkillManager
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


def test_skill_manager_reuses_parsed_skills_and_invalidates_index_on_changes(tmp_path, monkeypatch):
    manager = SkillManager([tmp_path])
    created = manager.create_skill(
        "Cached Skill",
        "---\ndescription: Original cached workflow description.\n---\n\n# Cached\nOriginal steps.",
        category="demo",
    )
    original_parse = manager.parse_skill_file
    parsed: list[str] = []

    def spy_parse(path):
        parsed.append(str(path))
        return original_parse(path)

    monkeypatch.setattr(manager, "parse_skill_file", spy_parse)
    manager.list_all()
    first_parse_count = len(parsed)
    assert first_parse_count > 0

    # The second prompt-facing lookup observes signatures but does not reread
    # and parse every unchanged SKILL.md file.
    manager.list_all()
    first_index = manager.compact_index()
    second_index = manager.compact_index()
    assert len(parsed) == first_parse_count
    assert first_index == second_index

    created.path.write_text(
        "---\nname: cached-skill\ndescription: Updated cached workflow with longer description.\n---\n\n# Cached\nUpdated steps.",
        encoding="utf-8",
    )
    updated_index = manager.compact_index()
    assert len(parsed) == first_parse_count + 1
    assert "Updated cached workflow" in updated_index

    # CRUD invalidation is immediate rather than waiting for a later directory
    # scan, so the prompt index cannot retain deleted/old instructions.
    manager.update_skill(
        "cached-skill",
        "---\ndescription: CRUD refreshed workflow.\n---\n\n# Cached\nFresh steps.",
    )
    assert "CRUD refreshed workflow" in manager.compact_index()
    assert manager.delete_skill("cached-skill") is True
    assert "cached-skill" not in manager.compact_index()


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
    assert {
        "code-review", "web-research", "daily-planner", "computer-use", "browser-use",
        "browser-form-workflow", "browser-content-review", "conversation-conduct",
    }.issubset(names)
    assert "Select by the meaning of the complete task" in manager.compact_index()

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

    browser_skills = {skill.name for skill in manager.relevant_skills("open Instagram using MCP")}
    assert "browser-use" in browser_skills
    assert "computer-use" not in browser_skills
    browser_use = next(skill for skill in manager.relevant_skills("open Instagram using MCP") if skill.name == "browser-use")
    assert manager.selection_reason(browser_use, "open Instagram using MCP") == "matches a browser action request"

    dm_skills = {
        skill.name for skill in manager.relevant_skills("read my latest dms in insta")
    }
    media_skills = {
        skill.name
        for skill in manager.relevant_skills(
            "use plawright mcp to play pal pal by tailwinder"
        )
    }
    assert "browser-use" in dm_skills
    assert "web-research" not in dm_skills
    assert "browser-use" in media_skills


@pytest.mark.parametrize(
    "user_input",
    [
        "How many agents did you use for the parallel search?",
        "Explain how web search works.",
        "Compare these search results.",
        "Did the researcher search the web?",
        "What tools did the agents use?",
    ],
)
def test_search_word_without_browser_action_never_loads_browser_skill(tmp_path, user_input):
    manager = SkillManager([tmp_path])

    selected = manager.relevant_skills(user_input)

    assert "browser-use" not in {skill.name for skill in selected}


def test_skill_router_requires_browser_action_and_target_and_routes_other_intents(tmp_path):
    manager = SkillManager([tmp_path])

    browser = manager.relevant_skills("Search this website by clicking the search box")
    research = manager.relevant_skills("Compare these search results and evaluate the sources")
    codebase = manager.relevant_skills("Explain this codebase")

    assert browser[0].name == "browser-use"
    assert manager.selection_reason(browser[0], "Search this website by clicking the search box") == "matches a browser action request"
    assert "web-research" in {skill.name for skill in research}
    assert "browser-use" not in {skill.name for skill in research}
    assert "codebase-summary" in {skill.name for skill in codebase}
    for casual in ("hey", "thanks", "okay"):
        assert manager.relevant_skills(casual) == []


def test_browser_request_does_not_autoload_generic_research_or_code_skills(tmp_path):
    manager = SkillManager([tmp_path])

    relevant = manager.relevant_skills(
        "open Instagram, go to the group, and summarize the latest message"
    )

    assert [skill.name for skill in relevant] == ["browser-use", "browser-content-review"]


def test_skill_selection_uses_only_explicitly_triggered_compatible_companions(tmp_path):
    manager = SkillManager([tmp_path])

    form = manager.relevant_skills("open the portal and fill the web form, then submit it")
    assert [skill.name for skill in form] == ["browser-use", "browser-form-workflow"]

    browser_reply = manager.relevant_skills("open the web chat and draft a reply, but do not send it")
    assert [skill.name for skill in browser_reply] == ["browser-use", "conversation-conduct"]

    standalone_reply = manager.relevant_skills("draft a concise reply to this message")
    assert [skill.name for skill in standalone_reply] == ["conversation-conduct"]

    plain_browser = manager.relevant_skills("open a website")
    assert [skill.name for skill in plain_browser] == ["browser-use"]

    assert "browser-form-workflow" not in {
        skill.name for skill in manager.relevant_skills("what is the latest in news")
    }


def test_browser_and_windows_skills_have_clear_non_overlapping_routes(tmp_path):
    manager = SkillManager([tmp_path])

    website = {skill.name for skill in manager.relevant_skills("open Google and fill the website form")}
    native_app = {skill.name for skill in manager.relevant_skills("open Notepad and save a desktop note")}
    visible_browser_window = {
        skill.name for skill in manager.relevant_skills("inspect the actual Chrome window on my desktop")
    }

    assert "browser-use" in website
    assert "computer-use" not in website
    assert "computer-use" in native_app
    assert "browser-use" not in native_app
    assert "computer-use" in visible_browser_window
    assert "browser-use" not in visible_browser_window
    browser_skill = manager.get_skill("browser-use")
    computer_skill = manager.get_skill("computer-use")
    assert browser_skill is not None and "Playwright MCP" in browser_skill.content
    assert computer_skill is not None and "Do not use this skill for\nnormal browser/web automation" in computer_skill.content


def test_explicit_windows_mcp_route_cannot_select_unrelated_telegram_download_skill(
    tmp_path,
):
    manager = SkillManager([tmp_path])
    manager.create_skill(
        "Download Song to Telegram",
        (
            "---\n"
            "description: Download a song from YouTube, search for it, and send it to Telegram.\n"
            "category: general\n"
            "---\n\n"
            "# Download Song\n"
            "Search YouTube, download audio, then send the file to Telegram.\n"
        ),
        category="general",
    )
    request = (
        "hey can you msg Sujal Mankar on Telegram using Windows MCP and send "
        "a greeting to him; you can search him on Telegram"
    )

    selected = manager.relevant_skills(request)

    assert [skill.name for skill in selected] == ["computer-use"]
    assert (
        manager.selection_reason(selected[0], request)
        == "matches a desktop action request"
    )
    assert "download-song-to-telegram" not in {
        skill.name
        for skill in manager.relevant_skills(
            "send a normal Telegram greeting to Sujal"
        )
    }


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
