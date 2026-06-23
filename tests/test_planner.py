"""Tests for the task planner module."""

import json
import pytest
from unittest.mock import AsyncMock
from ares.planner import TaskPlanner


def _make_planner(response_text):
    """Create a TaskPlanner with a mocked LLM that returns the given text."""
    mock_llm = AsyncMock()
    mock_llm.chat = AsyncMock(return_value={"content": response_text})
    return TaskPlanner(mock_llm)


class TestParsePlan:
    def test_parse_json_array(self):
        planner = _make_planner("[]")
        result = planner._parse_plan('[{"step": 1, "title": "Do thing", "description": "desc"}]')
        assert len(result) == 1
        assert result[0]["title"] == "Do thing"
        assert result[0]["status"] == "pending"

    def test_parse_markdown_code_block(self):
        planner = _make_planner("")
        text = 'Here is the plan:\n```json\n[{"step": 1, "title": "Step one", "description": "desc"}]\n```'
        result = planner._parse_plan(text)
        assert len(result) == 1
        assert result[0]["title"] == "Step one"

    def test_parse_inline_json(self):
        planner = _make_planner("")
        text = 'Plan: [{"step": 1, "title": "Search", "description": "find stuff"}]'
        result = planner._parse_plan(text)
        assert len(result) == 1

    def test_parse_invalid_raises(self):
        planner = _make_planner("")
        with pytest.raises(ValueError, match="Could not parse"):
            planner._parse_plan("no json here at all")

    def test_validate_renumbers_steps(self):
        planner = _make_planner("")
        result = planner._validate_plan([
            {"step": 5, "title": "A", "description": ""},
            {"step": 99, "title": "B", "description": ""},
        ])
        assert result[0]["step"] == 1
        assert result[1]["step"] == 2

    def test_validate_truncates_long_titles(self):
        planner = _make_planner("")
        result = planner._validate_plan([{"step": 1, "title": "x" * 100, "description": ""}])
        assert len(result[0]["title"]) == 60

    def test_validate_empty_list_raises(self):
        planner = _make_planner("")
        with pytest.raises(ValueError, match="empty"):
            planner._validate_plan([])


class TestFallbackPlan:
    def test_fallback_has_one_step(self):
        planner = _make_planner("")
        result = planner._fallback_plan({"title": "My task", "description": "desc"})
        assert len(result) == 1
        assert result[0]["title"] == "My task"
        assert result[0]["status"] == "pending"

    def test_fallback_uses_title_as_description(self):
        planner = _make_planner("")
        result = planner._fallback_plan({"title": "Do stuff"})
        assert result[0]["description"] == "Do stuff"


class TestGeneratePlan:
    @pytest.mark.asyncio
    async def test_generate_plan_returns_list(self):
        plan_data = [
            {"step": 1, "title": "Step one", "description": "desc one"},
            {"step": 2, "title": "Step two", "description": "desc two"},
        ]
        planner = _make_planner(json.dumps(plan_data))
        result = await planner.generate_plan({"title": "test", "description": "desc"})
        assert len(result) == 2
        assert result[0]["status"] == "pending"

    @pytest.mark.asyncio
    async def test_generate_plan_fallback_on_error(self):
        mock_llm = AsyncMock()
        mock_llm.chat = AsyncMock(side_effect=Exception("API error"))
        planner = TaskPlanner(mock_llm)
        result = await planner.generate_plan({"title": "fallback task"})
        assert len(result) == 1
        assert result[0]["title"] == "fallback task"
