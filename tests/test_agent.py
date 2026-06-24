"""Tests for the agent loop."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from ares.agent import Agent
from ares.memory import MemoryStore
from ares.models import AppConfig
from ares.tools.tasks import TaskStore


@pytest.fixture
def agent(tmp_path, fake_embedding_provider):
    mem_store = MemoryStore(db_path=tmp_path / "mem.db", embedding_provider=fake_embedding_provider)
    task_store = TaskStore(db_path=tmp_path / "tasks.db")
    return Agent(
        memory_store=mem_store,
        task_store=task_store,
        api_key="test-key",
        config=AppConfig(data_dir=str(tmp_path / "ares-data"), project_context_enabled=False),
    )


class TestAgent:
    def test_build_messages_includes_system_prompt(self, agent):
        """Messages include the system prompt."""
        messages = agent.build_messages("Hello", [])
        assert messages[0]["role"] == "system"
        assert "Ares" in messages[0]["content"]

    def test_build_messages_includes_user_input(self, agent):
        """Messages include the user's input."""
        messages = agent.build_messages("Hello", [])
        roles = [m["role"] for m in messages]
        assert "user" in roles

    def test_build_messages_includes_context(self, agent):
        """Messages include context when provided."""
        context = "## What I know about you:\n- prefers dark mode"
        messages = agent.build_messages("Hello", [], context=context)
        all_content = " ".join(m["content"] for m in messages)
        assert "dark mode" in all_content

    def test_get_context_includes_soul_profile_memory_and_tasks(self, agent):
        """Full context includes proactive layers plus memories and tasks."""
        agent.soul_manager.soul_path.write_text("## Personality\nBe direct.", encoding="utf-8")
        agent.profile_manager.profile_path.write_text("## Identity\nName: Alice", encoding="utf-8")
        agent.memory_store.store("User likes tea", category="preference")
        agent.task_store.create("Buy milk")

        context = agent.get_context("tea")

        assert "Ares Personality" in context
        assert "Be direct" in context
        assert "User Profile" in context
        assert "Alice" in context
        assert "User likes tea" in context
        assert "Buy milk" in context

    def test_process_tool_calls_store_memory(self, agent, tmp_path):
        """Processing a store_memory tool call stores the fact."""
        tool_call = {
            "id": "call_1",
            "function": {
                "name": "store_memory",
                "arguments": json.dumps({"content": "User likes pizza", "category": "preference"}),
            },
        }
        results = agent.process_tool_calls([tool_call])
        assert len(results) == 1
        assert results[0]["tool_name"] == "store_memory"
        assert "pizza" in results[0]["content"]

    def test_process_tool_calls_create_task(self, agent):
        """Processing a create_task tool call creates a task."""
        tool_call = {
            "id": "call_2",
            "function": {
                "name": "create_task",
                "arguments": json.dumps({"title": "Buy milk", "priority": "high"}),
            },
        }
        results = agent.process_tool_calls([tool_call])
        assert len(results) == 1
        assert results[0]["tool_name"] == "create_task"
        assert "milk" in results[0]["content"]


    def _auto_task_agent(self, tmp_path):
        """Build a minimal Agent instance without constructing MemoryStore."""
        from ares.tools import ToolExecutor

        task_store = TaskStore(db_path=tmp_path / "tasks.db")
        agent = object.__new__(Agent)
        agent.memory_store = SimpleNamespace(search=lambda query, limit=5: [])
        agent.task_store = task_store
        agent.conversation_store = None
        agent.config = AppConfig(data_dir=str(tmp_path / "ares-data"), project_context_enabled=False)
        agent.tool_executor = ToolExecutor(
            memory_store=agent.memory_store,
            task_store=task_store,
            conversation_store=None,
        )
        agent.tools = []
        agent.get_context = lambda user_input: ""
        return agent

    def test_process_tool_calls_auto_task_skips_inline_followups(self, tmp_path):
        """Auto-executable task creation prevents same-turn inline execution."""
        agent = self._auto_task_agent(tmp_path)
        tool_calls = [
            {
                "id": "call_task",
                "function": {
                    "name": "create_task",
                    "arguments": json.dumps({"title": "Write script", "auto_executable": True}),
                },
            },
            {
                "id": "call_inline",
                "function": {
                    "name": "run_command",
                    "arguments": json.dumps({"command": "echo should-not-run"}),
                },
            },
        ]

        results = agent.process_tool_calls(tool_calls)

        assert results[0]["tool_name"] == "create_task"
        assert results[0]["auto_task_created"] is True
        assert "background executor" in results[0]["content"].lower()
        assert results[1]["tool_name"] == "run_command"
        assert results[1]["skipped_after_auto_task"] is True
        assert "Skipped" in results[1]["content"]

    def test_run_stops_after_auto_executable_task_creation(self, tmp_path):
        """The main agent loop does not let the LLM execute an auto-task inline."""
        import asyncio

        agent = self._auto_task_agent(tmp_path)

        class FakeLLM:
            config = agent.config

            def __init__(self):
                self.calls = 0

            async def chat(self, messages, tools=None):
                self.calls += 1
                if self.calls == 1:
                    return {
                        "content": "",
                        "tool_calls": [{
                            "id": "call_task",
                            "function": {
                                "name": "create_task",
                                "arguments": json.dumps({"title": "Write script", "auto_executable": True}),
                            },
                        }],
                    }
                return {
                    "content": "",
                    "tool_calls": [{
                        "id": "call_inline",
                        "function": {
                            "name": "run_command",
                            "arguments": json.dumps({"command": "echo should-not-run"}),
                        },
                    }],
                }

            async def close(self):
                pass

        agent.llm = FakeLLM()

        async def collect():
            return [chunk async for chunk in agent.run("create an auto task", [])]

        chunks = asyncio.run(collect())

        assert agent.llm.calls == 1
        assert "background executor" in "".join(chunks).lower()

    def test_process_tool_calls_list_tasks(self, agent):
        """Processing list_tasks returns task list."""
        agent.task_store.create("Existing task")
        tool_call = {
            "id": "call_3",
            "function": {"name": "list_tasks", "arguments": "{}"},
        }
        results = agent.process_tool_calls([tool_call])
        assert len(results) == 1
        assert results[0]["tool_name"] == "list_tasks"
        assert "existing task" in results[0]["content"].lower()

    def test_process_tool_calls_malformed_json_returns_error(self, agent):
        """Malformed tool arguments are reported without crashing the agent."""
        tool_call = {
            "id": "call_bad",
            "function": {"name": "list_tasks", "arguments": "{"},
        }
        results = agent.process_tool_calls([tool_call])
        assert len(results) == 1
        assert results[0]["tool_call_id"] == "call_bad"
        assert results[0]["tool_name"] == "list_tasks"
        assert "error" in results[0]["content"].lower()

    def test_tool_messages_strip_local_metadata(self, agent):
        """Local renderer metadata is not sent back to the LLM API."""
        messages = agent._tool_messages([{
            "tool_call_id": "call_1",
            "role": "tool",
            "content": "Stored memory #1",
            "tool_name": "store_memory",
        }])

        assert messages == [{
            "tool_call_id": "call_1",
            "role": "tool",
            "content": "Stored memory #1",
        }]
