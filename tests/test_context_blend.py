"""Tests for token estimation, truncation, and context blending."""

from ares.context_blend import (
    build_context_prompt,
    estimate_tokens,
    format_memories,
    format_summaries,
    format_tasks,
    truncate_to_tokens,
)


class TestEstimateTokens:
    def test_empty_string(self):
        assert estimate_tokens("") == 0

    def test_single_word(self):
        assert estimate_tokens("hello") == 1

    def test_multiple_words(self):
        assert estimate_tokens("hello world foo bar") == 5


class TestTruncateToTokens:
    def test_short_text_unchanged(self):
        assert truncate_to_tokens("hello world", max_tokens=100) == "hello world"

    def test_long_text_truncated_with_note(self):
        text = " ".join(f"word{i}" for i in range(200))
        result = truncate_to_tokens(text, max_tokens=50)
        assert "truncated" in result.lower()
        assert len(result) < len(text)


class TestFormatters:
    def test_format_memories_empty(self):
        assert format_memories([]) == ""

    def test_format_memories_with_category(self):
        result = format_memories([{
            "fact_id": 1,
            "fact_text": "likes tea",
            "category": "preference",
            "importance": 0.8,
        }])
        assert "likes tea" in result
        assert "preference" in result

    def test_format_tasks_empty(self):
        assert format_tasks([]) == ""

    def test_format_tasks_with_due_date(self):
        result = format_tasks([{"title": "Call mom", "due": "2026-06-20T14:00:00"}])
        assert "Call mom" in result
        assert "2026-06-20" in result

    def test_format_summaries_empty(self):
        assert format_summaries([]) == ""

    def test_format_summaries_list(self):
        result = format_summaries(["Discussed project setup", "Fixed login bug"])
        assert "project setup" in result
        assert "login bug" in result


class TestBuildContextPrompt:
    def test_empty_context(self):
        assert build_context_prompt() == ""

    def test_priority_ordering(self):
        result = build_context_prompt(
            soul_context="SOUL HERE",
            profile_context="PROFILE HERE",
            project_context="PROJECT HERE",
            conversation_summaries=["SUMMARY HERE"],
            memories=[{"fact_id": 1, "fact_text": "fact1", "category": "note"}],
            tasks=[{"title": "TASK HERE"}],
        )
        assert result.index("SOUL") < result.index("PROFILE") < result.index("PROJECT")
        assert result.index("SUMMARY") < result.index("fact1") < result.index("TASK")

    def test_token_budget_respected_for_large_profile(self):
        result = build_context_prompt(profile_context="x " * 500, token_budget=100)
        assert len(result.split()) < 200
        assert "truncated" in result.lower()

    def test_tasks_are_included_at_end(self):
        result = build_context_prompt(tasks=[{"title": "Buy milk", "due": None}])
        assert result.endswith("- Buy milk")
