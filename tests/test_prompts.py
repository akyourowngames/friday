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


def test_store_memory_tool_discourages_junk_memory():
    tools = get_tool_definitions()
    store = next(tool for tool in tools if tool["function"]["name"] == "store_memory")
    description = store["function"]["description"]

    assert "durable user preference" in description
    assert "Do not store temporary moods" in description
    assert "tool outputs" in description
