"""Tests for the agent loop."""

import asyncio
import json

import pytest

from ares.agent import Agent
from ares.memory import MemoryStore
from ares.models import AppConfig
from ares.skills.reflection import ReflectionService
from ares.skills.discovery import SkillManager
from ares.integrations.turn_policy import build_turn_execution_context


@pytest.fixture
def agent(tmp_path, fake_embedding_provider):
    mem_store = MemoryStore(db_path=tmp_path / "mem.db", embedding_provider=fake_embedding_provider)
    return Agent(
        memory_store=mem_store,
        api_key="test-key",
        config=AppConfig(data_dir=str(tmp_path / "ares-data"), project_context_enabled=False),
    )


class TestAgent:
    @pytest.mark.parametrize(
        "mode_kwargs",
        [{"is_cron_session": True}, {"is_voice_session": True}],
    )
    def test_cron_and_voice_agents_own_configured_mcp_runtime(
        self, tmp_path, fake_embedding_provider, mode_kwargs
    ):
        config = AppConfig(
            data_dir=str(tmp_path / "ares-data"),
            project_context_enabled=False,
            mcp_servers=[{
                "name": "calendar",
                "server_url": "https://example.test/mcp",
            }],
        )
        config.reflection.enabled = False
        memory = MemoryStore(
            db_path=tmp_path / f"{next(iter(mode_kwargs))}.db",
            embedding_provider=fake_embedding_provider,
        )

        owned = Agent(memory_store=memory, config=config, **mode_kwargs)

        assert owned._owns_mcp_manager is True
        assert set(owned.mcp_manager.servers) == {"calendar"}
        assert owned.tool_executor.mcp_manager is owned.mcp_manager
        asyncio.run(owned.close())
        memory.close()

    def test_pre_turn_mcp_recovery_refreshes_reconnected_schemas(self, agent):
        class FakeManager:
            def __init__(self):
                self.ensure_calls = 0
                self.tool_definitions = []

            async def ensure_running(self):
                self.ensure_calls += 1
                self.tool_definitions = [{
                    "type": "function",
                    "function": {
                        "name": "mcp__calendar__list_events",
                        "description": "List calendar events",
                        "parameters": {"type": "object", "properties": {}},
                    },
                }]
                return {"ready": True}

        manager = FakeManager()
        agent.set_mcp_manager(manager)

        asyncio.run(agent._ensure_mcp_connections())

        assert manager.ensure_calls == 1
        assert any(
            tool["function"]["name"] == "mcp__calendar__list_events"
            for tool in agent.tools
        )

    def test_pre_turn_mcp_recovery_only_wakes_the_routed_surface(self, agent):
        class FakeManager:
            def __init__(self):
                self.servers = {
                    "playwright": object(),
                    "windows": object(),
                    "github": object(),
                }
                self.tool_definitions = []
                self.calls = []

            async def ensure_server_running(self, name):
                self.calls.append(name)
                return {"name": name, "ready": True}

        manager = FakeManager()
        agent.set_mcp_manager(manager)

        asyncio.run(agent._ensure_mcp_connections(
            build_turn_execution_context("Click the search box on this website")
        ))
        asyncio.run(agent._ensure_mcp_connections(
            build_turn_execution_context("Open Notepad and type a note")
        ))

        assert manager.calls == ["playwright", "windows"]

    def test_reflection_outcome_summary_uses_real_tool_results(self):
        payload = json.loads(Agent._reflection_outcome_summary(
            [
                {"tool_name": "shell", "content": "12 passed"},
                {"tool_name": "deploy", "content": "Error: connection refused"},
            ],
            {"status": "partial", "request_id": "req-1"},
        ))

        assert payload["tool_outcomes"] == [
            {"tool": "shell", "status": "completed", "result": "12 passed"},
            {"tool": "deploy", "status": "failed", "result": "Error: connection refused"},
        ]
        assert payload["execution_record"]["request_id"] == "req-1"

    def test_configured_fast_model_routes_all_turns_without_changing_primary(self, agent):
        agent.config.fast_conversation_enabled = True
        agent.config.fast_conversation_model = "deepseek-v4-flash-free"
        agent.llm.model = "big-pickle"
        conversation = build_turn_execution_context("hey")
        substantive = build_turn_execution_context("Write and test a Python file")

        assert agent._tools_for_turn(conversation) == []
        assert agent._model_for_turn(conversation, []) == "deepseek-v4-flash-free"
        assert (
            agent._model_for_turn(substantive, agent._tools_for_turn(substantive))
            == "deepseek-v4-flash-free"
        )
        assert agent.llm.model == "big-pickle"

    def test_reflection_nested_config_does_not_become_an_llm_config(self, agent, monkeypatch):
        created_with = []

        class StubLLM:
            def __init__(self, config=None):
                created_with.append(config)

        monkeypatch.setattr("ares.skills.reflection.LLMClient", StubLLM)
        service = ReflectionService(
            memory_store=agent.memory_store,
            goal_store=agent.goal_store,
            commitment_store=agent.commitment_store,
            profile_manager=agent.profile_manager,
            config=agent.config.reflection,
        )

        assert created_with == [None]
        assert service.llm.__class__ is StubLLM

    def test_session_scope_isolates_memory_and_tool_provenance(self, agent, monkeypatch):
        searches = []

        def search(query, **kwargs):
            searches.append((query, kwargs))
            return []

        monkeypatch.setattr(agent.memory_store, "search", search)
        agent.set_session_id("default-session")

        with agent.session_scope("conversation-42"):
            assert agent.session_id == "conversation-42"
            assert agent.tool_executor.session_id == "conversation-42"
            agent.get_context("isolated fact")

        # Conversation scope keeps temporary rows local while session search
        # still includes durable global memories (session_id=NULL).
        assert searches[-1][1]["scope"] == "session"
        assert searches[-1][1]["session_id"] == "conversation-42"
        assert agent.session_id == "default-session"
        assert agent.tool_executor.session_id == "default-session"

    def test_follow_up_is_visible_and_cancellable_from_later_conversation(self, agent):
        follow_up = agent.follow_up_store.create(
            "Check whether the staged rollout completed",
            confidence=0.94,
            source_conversation_id="conversation-1",
            source_reflection_id="reflection-1",
            eligible_at="2026-07-16T09:00:00+05:30",
        )
        agent.set_session_id("conversation-99")

        context = agent.get_context("What follow-ups are still pending?")
        listed = json.loads(agent.tool_executor.execute("list_follow_ups", {"limit": 10}))
        cancelled = json.loads(agent.tool_executor.execute(
            "resolve_follow_up",
            {
                "follow_up_id": follow_up["follow_up_id"],
                "status": "cancelled",
                "resolution": "The user cancelled this check-in.",
            },
        ))

        assert "## Pending Follow-ups:" in context
        assert "staged rollout completed" in context
        assert listed["follow_ups"][0]["source_conversation_id"] == "conversation-1"
        assert cancelled["follow_up"]["status"] == "cancelled"
        assert agent.follow_up_store.list_open() == []

    def test_build_messages_includes_system_prompt(self, agent):
        """Messages include the system prompt."""
        messages = agent.build_messages("Hello", [])
        assert messages[0]["role"] == "system"
        assert "Ares" in messages[0]["content"]
        assert "## Tool Routing" in messages[0]["content"]
        assert "Playwright/browser MCP" in messages[0]["content"]

    def test_build_messages_includes_runtime_clock_context(self, agent):
        """System prompt includes dynamic runtime clock context."""
        messages = agent.build_messages("What day is it?", [])
        system = messages[0]["content"]

        assert "## Runtime" in system
        assert "Current local date:" in system
        assert "Timezone:" in system

    def test_build_messages_starts_semantic_computer_task_for_desktop_turn(self, agent):
        context = build_turn_execution_context(
            'Send a Telegram message to Sujal Mankar saying "Call me at 6"'
        )

        with agent.turn_scope(context):
            messages = agent.build_messages(context.user_input, [])

        system = messages[0]["content"]
        assert "## Live Computer Task" in system
        assert "Immutable target: 'Sujal Mankar'" in system
        assert "Immutable message: 'Call me at 6'" in system
        assert "ui_generation" in system

    def test_build_messages_uses_live_mcp_state_over_stale_history(self, agent):
        class FakeManager:
            tool_definitions = [{
                "type": "function",
                "function": {
                    "name": "mcp__windows__Snapshot",
                    "description": "Inspect the desktop",
                    "parameters": {"type": "object", "properties": {}},
                },
            }]

            @staticmethod
            def readiness_report():
                return {
                    "servers": {
                        "windows": {"ready": True},
                        "calendar": {"ready": False},
                    }
                }

        agent.mcp_manager = FakeManager()
        messages = agent.build_messages(
            "Use Windows MCP now",
            [{"role": "assistant", "content": "Windows MCP was unavailable earlier."}],
        )

        guard = messages[-2]["content"]
        assert "Live MCP State" in guard
        assert "Ready now: windows" in guard
        assert "overrides older assistant messages" in guard
        assert any(tool["function"]["name"] == "mcp__windows__Snapshot" for tool in agent.tools)

    def test_build_messages_includes_user_input(self, agent):
        """Messages include the user's input."""
        messages = agent.build_messages("Hello", [])
        roles = [m["role"] for m in messages]
        assert "user" in roles
        assert messages[-2]["role"] == "system"
        assert "Current Turn Guard" in messages[-2]["content"]
        assert messages[-1] == {"role": "user", "content": "Hello"}

    def test_build_messages_includes_context(self, agent):
        """Messages include context when provided."""
        context = "## What I know about you:\n- prefers dark mode"
        messages = agent.build_messages("Hello", [], context=context)
        all_content = " ".join(m["content"] for m in messages)
        assert "dark mode" in all_content

    def test_build_messages_auto_loads_relevant_skill_in_system_context(self, agent, tmp_path):
        """Relevant skills are hidden working instructions, not user-visible chatter."""
        skill_dir = tmp_path / "coding" / "review-diff"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            (
                "---\n"
                "name: review-diff\n"
                "description: Review git diffs and flag risky code changes. Use when the user asks for review or risk.\n"
                "category: coding\n"
                "---\n\n"
                "# Review Diff\n"
                "Always list serious findings first.\n"
            ),
            encoding="utf-8",
        )
        agent.skill_manager = SkillManager([tmp_path])

        messages = agent.build_messages("review my diff for risk", [])
        system = messages[0]["content"]

        assert "## Auto-Loaded Skills" in system
        assert "# Skill: review-diff" in system
        assert "Always list serious findings first." in system
        assert "Do not mention skill loading" in system

    def test_build_messages_respects_skill_auto_suggest_flag(self, agent, tmp_path):
        skill_dir = tmp_path / "coding" / "review-diff"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\nname: review-diff\ndescription: Review git diffs and risk.\n---\n\n# Review\nDo it.",
            encoding="utf-8",
        )
        agent.skill_manager = SkillManager([tmp_path])
        agent.config.skill_auto_suggest = False

        messages = agent.build_messages("review my diff for risk", [])

        assert "## Auto-Loaded Skills" not in messages[0]["content"]

    def test_get_context_includes_soul_profile_memory(self, agent):
        """Full context includes proactive layers plus memories."""
        agent.soul_manager.soul_path.write_text("## Personality\nBe direct.", encoding="utf-8")
        agent.profile_manager.profile_path.write_text("## Identity\nName: Alice", encoding="utf-8")
        agent.memory_store.store("User likes tea", category="preference")

        context = agent.get_context("tea")

        assert "Ares Personality" in context
        assert "Be direct" in context
        assert "User Profile" in context
        assert "Alice" in context
        assert "User likes tea" in context

    def test_process_tool_calls_store_memory(self, agent, tmp_path):
        """Processing a store_memory tool call stores the fact."""
        tool_call = {
            "id": "call_1",
            "function": {
                "name": "store_memory",
                "arguments": json.dumps({"content": "User likes pizza", "category": "preference"}),
            },
        }
        with agent.turn_scope(build_turn_execution_context("Remember that I like pizza")):
            results = agent.process_tool_calls([tool_call])
        assert len(results) == 1
        assert results[0]["tool_name"] == "store_memory"
        assert "pizza" in results[0]["content"]

    def test_process_tool_calls_malformed_json_returns_error(self, agent):
        """Malformed tool arguments are reported without crashing the agent."""
        tool_call = {
            "id": "call_bad",
            "function": {"name": "store_memory", "arguments": "{"},
        }
        results = agent.process_tool_calls([tool_call])
        assert len(results) == 1
        assert results[0]["tool_call_id"] == "call_bad"
        assert results[0]["tool_name"] == "store_memory"
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


def test_build_messages_without_skill_manager_does_not_crash(tmp_path):
    agent = object.__new__(Agent)
    agent.config = AppConfig(data_dir=str(tmp_path), skills_enabled=True)
    messages = agent.build_messages("hello", [])
    assert messages[-1] == {"role": "user", "content": "hello"}


def test_voice_session_filters_cron_tools():
    from ares.agent import Agent
    from ares.memory import MemoryStore

    agent = Agent(memory_store=MemoryStore(), is_voice_session=True)
    tool_names = {tool["function"]["name"] for tool in agent.tools}
    cron_tools = {
        "create_cron_job",
        "list_cron_jobs",
        "get_cron_job",
        "update_cron_job",
        "delete_cron_job",
        "run_cron_job_now",
        "get_cron_logs",
    }

    assert not tool_names.intersection(cron_tools)


def test_non_voice_session_keeps_cron_tools():
    from ares.agent import Agent
    from ares.memory import MemoryStore

    agent = Agent(memory_store=MemoryStore(), is_voice_session=False)
    tool_names = {tool["function"]["name"] for tool in agent.tools}

    assert "list_cron_jobs" in tool_names or "create_cron_job" in tool_names
