"""Tests for token estimation, truncation, and context blending."""

from ares.context.blend import (
    CONTEXT_WINDOWS,
    build_context_prompt,
    estimate_token_breakdown,
    estimate_tokens,
    format_memories,
    format_goals,
    format_summaries,
    get_model_budgets,
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

    def test_format_summaries_empty(self):
        assert format_summaries([]) == ""

    def test_format_summaries_list(self):
        result = format_summaries(["Discussed project setup", "Fixed login bug"])
        assert "project setup" in result
        assert "login bug" in result

    def test_format_summaries_preserves_saved_contact_values(self):
        result = format_summaries(["Contact me at private@example.test or +1 555 555 0123"])
        assert "private@example.test" in result
        assert "+1 555 555 0123" in result
        assert "[redacted email]" not in result

    def test_format_goals_includes_progress_mode_and_due_state(self):
        goal = {"goal_id": 3, "title": "Ship Ares", "status": "active", "priority": "high", "progress_percent": 30, "progress_mode": "derived", "target_date": "2026-07-20", "days_remaining": 4, "next_action": "Run release checks", "blockers": [{"description": "Waiting for review"}]}
        result = format_goals([goal], [goal], [])
        assert "Ship Ares" in result
        assert "30% progress (derived)" in result
        assert "Due soon" in result
        assert "Run release checks" in result
        assert "Waiting for review" in result


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
        )
        assert result.index("SOUL") < result.index("PROFILE") < result.index("PROJECT")

    def test_token_budget_respected_for_large_profile(self):
        result = build_context_prompt(profile_context="x " * 500, token_budget=100)
        assert len(result.split()) < 200
        assert "truncated" in result.lower()

class TestGetModelBudgets:
    """Test model-aware context budget scaling."""

    def test_small_model_128k(self):
        budgets = get_model_budgets("deepseek-v4-flash-free")
        assert budgets["context_window"] == 128_000
        assert budgets["context_token_budget"] == 4_000
        assert budgets["max_memory_retrieval"] == 8
        assert 0 < budgets["compact_threshold"] <= 1.0
        assert budgets["max_context_messages"] >= 10

    def test_medium_model_200k(self):
        budgets = get_model_budgets("claude-opus-4-8")
        assert budgets["context_window"] == 200_000
        assert budgets["context_token_budget"] == 8_000
        assert budgets["max_memory_retrieval"] == 15

    def test_large_model_1m(self):
        budgets = get_model_budgets("gemini-3.5-flash")
        assert budgets["context_window"] == 1_000_000
        assert budgets["context_token_budget"] == 32_000
        assert budgets["max_memory_retrieval"] == 30

    def test_unknown_model_defaults(self):
        budgets = get_model_budgets("totally-unknown-model-xyz")
        assert budgets["context_window"] == 128_000
        assert budgets["context_token_budget"] == 4_000

    def test_buffer_is_positive(self):
        for model in ("deepseek-v4-flash-free", "claude-opus-4-8", "gemini-3.5-flash"):
            budgets = get_model_budgets(model)
            assert budgets["buffer"] > 0
            assert budgets["usable_window"] > 0
            assert budgets["usable_window"] < budgets["context_window"]

    def test_compact_threshold_in_reasonable_range(self):
        budgets = get_model_budgets("gpt-5.4")
        assert 0.5 <= budgets["compact_threshold"] <= 1.0


class TestEstimateTokenBreakdown:
    """Test token breakdown estimation for context bar display."""

    def test_empty_history(self):
        result = estimate_token_breakdown("Hello world", [])
        assert result["system_prompt"] >= 0
        assert result["history"] == 0
        assert result["tool_output"] == 0
        assert result["total"] >= 0

    def test_with_history_messages(self):
        history = [
            {"role": "user", "content": "Hello there, how are you doing today?"},
            {"role": "assistant", "content": "I'm doing great, thanks for asking!"},
        ]
        result = estimate_token_breakdown("System prompt text here", history)
        assert result["history"] > 0
        assert result["system_prompt"] > 0
        assert result["total"] == result["system_prompt"] + result["history"] + result["tool_output"]

    def test_with_tool_outputs(self):
        history = [{"role": "user", "content": "Run the tool"}]
        tool_outputs = ["Tool returned a long result with lots of data in it"]
        result = estimate_token_breakdown("System", history, tool_outputs=tool_outputs)
        assert result["tool_output"] > 0


class TestContextWindows:
    """Test that CONTEXT_WINDOWS covers expected model families."""

    def test_has_claude_models(self):
        assert "claude-opus-4-8" in CONTEXT_WINDOWS
        assert "claude-fable-5" in CONTEXT_WINDOWS

    def test_has_gpt_models(self):
        assert "gpt-5.4" in CONTEXT_WINDOWS
        assert "gpt-4o" in CONTEXT_WINDOWS

    def test_has_gemini_large_context(self):
        assert CONTEXT_WINDOWS["gemini-3.5-flash"] == 1_000_000

    def test_has_free_models(self):
        assert "deepseek-v4-flash-free" in CONTEXT_WINDOWS
        assert "mimo-v2.5-free" in CONTEXT_WINDOWS
