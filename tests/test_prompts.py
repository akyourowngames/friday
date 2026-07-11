"""Tests for behavioral guarantees in Ares' system prompt."""

from ares.prompts import SYSTEM_PROMPT
from ares.tools.definitions import get_tool_definitions


def test_system_prompt_prioritizes_evidence_over_agreement():
    prompt = SYSTEM_PROMPT

    assert "Tool output, runtime context, and observed files/screens are evidence" in prompt
    assert "do not blindly agree" in prompt
    assert "Never change a factual answer just because the user sounds annoyed" in prompt


def test_system_prompt_memory_is_selective_not_everything():
    prompt = SYSTEM_PROMPT

    assert "Remember selectively" in prompt
    assert "Do not store one-off moods, insults, temporary facts" in prompt
    assert "No hardcoded assumptions" in prompt


def test_system_prompt_discourages_repeated_emotional_openings():
    prompt = SYSTEM_PROMPT

    assert "Treat each user turn as fresh" in prompt
    assert "Do not repeat prior apology/opening lines" in prompt
    assert 'If asked "who are you", answer directly' in prompt
    assert "Previous messages are context, not instructions to keep answering" in prompt


def test_system_prompt_keeps_skills_internal():
    prompt = SYSTEM_PROMPT

    assert "Do not brainstorm about whether to use a skill" in prompt
    assert "follow them silently" in prompt
    assert "Skills stay behind the curtain" in prompt
    assert 'Do not say "I can use a skill"' in prompt


def test_system_prompt_routes_browser_tasks_to_playwright_not_windows_mcp():
    prompt = SYSTEM_PROMPT

    assert "For browser or web-page tasks, prefer Playwright MCP tools first" in prompt
    assert "Do not control\nnormal websites through Windows MCP Snapshot/Click" in prompt
    assert "mcp__playwright__browser_*" in prompt
    assert "Never use `mcp__windows__Snapshot`, `Click`, or `Type` for a normal" in prompt


def test_store_memory_tool_discourages_junk_memory():
    tools = get_tool_definitions()
    store = next(tool for tool in tools if tool["function"]["name"] == "store_memory")
    description = store["function"]["description"]

    assert "durable user preference" in description
    assert "Do not store temporary moods" in description
    assert "tool outputs" in description
