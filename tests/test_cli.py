"""Tests for CLI startup helpers."""

import json
import os
import sqlite3
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

import pytest
from rich.console import Console

from ares.cli import app as cli_module
from ares.__main__ import _run_coro
from ares.cli import AresCLI, _history_path
from ares.models import AppConfig
from ares.tools.mcp_client import MCPServerConfig


async def _answer() -> int:
    return 42


class DummyAgent:
    def __init__(self):
        self.model = None
        self.last_messages = []

    def set_model(self, model: str) -> None:
        self.model = model


class StreamingAgent(DummyAgent):
    async def run_stream(self, *_args, **_kwargs):
        payload = {
            "query": "bitcoin price",
            "provider": "tavily",
            "summary": "Bitcoin is moving today.",
            "results": [{
                "title": "Bitcoin price today",
                "url": "https://example.com/btc",
                "snippet": "Latest Bitcoin market price.",
            }],
            "errors": [],
        }
        yield "[tool_start:web_search]"
        yield f"[tool:web_search:{json.dumps(payload)}]"
        yield "Done."


class MemoryStreamingAgent(DummyAgent):
    async def run_stream(self, *_args, **_kwargs):
        yield "[tool_start:store_memory]"
        yield "[tool:store_memory:Stored memory #7: user secret phrase]"
        yield "Saved."


class MCPStreamingAgent(DummyAgent):
    async def run_stream(self, *_args, **_kwargs):
        snapshot = "\n".join(f"button Save row {index}" for index in range(240))
        yield "[tool_start:mcp__playwright__browser_snapshot]"
        yield f"[tool:mcp__playwright__browser_snapshot:{snapshot}]"
        yield "Snapshot reviewed."


class SkillStreamingAgent(DummyAgent):
    def __init__(self):
        super().__init__()
        skill = SimpleNamespace(
            name="computer-use",
            description="Operate desktop apps with observe and verify steps.",
        )
        self.skill_manager = SimpleNamespace(relevant_skills=lambda _query: [skill])

    async def run_stream(self, *_args, **_kwargs):
        yield "Ready to work."


class EmojiStreamingAgent(DummyAgent):
    async def run_stream(self, *_args, **_kwargs):
        yield "Hey there 👋 What's up? 😊"


class LongStreamingAgent(DummyAgent):
    async def run_stream(self, *_args, **_kwargs):
        yield (
            "LDR: Hot and humid right now, but heavy rain and thunderstorms are coming "
            "tonight. Stay indoors if you can, and keep an umbrella handy!\n\n"
            "- High: 35C / Low: 25C with a long note that should wrap cleanly inside "
            "the assistant gutter."
        )


class TableStreamingAgent(DummyAgent):
    async def run_stream(self, *_args, **_kwargs):
        yield (
            "Headlines\n\n"
            "| Story | When |\n"
            "|---|---|\n"
            "| Match update | Today |\n"
        )


class ColorLeakStreamingAgent(DummyAgent):
    async def run_stream(self, *_args, **_kwargs):
        yield "\x1b[36mThis should not leak cyan\x1b[0m and the final word survives: scoreboard"


class RepeatedOpeningAgent(DummyAgent):
    async def run_stream(self, *_args, **_kwargs):
        yield (
            "Haha fair, I deserve that one\n\n"
            "I'm Ares, your personal AI assistant in the terminal."
        )


class DummyMemoryStore:
    def __init__(self):
        self.memories = {
            12: {
                "fact_id": 12,
                "fact_text": "User likes tea",
                "category": "preference",
                "importance": 0.5,
                "updated_at": "now",
            }
        }

    def get_recent(self, limit=10):
        return list(self.memories.values())[:limit]

    def list_all(self):
        return list(self.memories.values())

    def search(self, query, limit=10):
        return [m for m in self.memories.values() if query.lower() in m["fact_text"].lower()]

    def update(self, fact_id, *, fact_text=None, **_):
        if fact_id not in self.memories:
            return False
        if fact_text:
            self.memories[fact_id]["fact_text"] = fact_text
        return True

    def delete(self, fact_id):
        return self.memories.pop(fact_id, None) is not None


class DummySoulManager:
    soul_path = Path("soul.md")

    def read(self):
        return "# Soul\nBe concise."

    def get_context(self, token_budget=200):
        return "## Ares Personality\nBe concise."


class DummyProfileManager:
    profile_path = Path("profile.md")

    def read(self):
        return "# About Me\nName: Alice"

    def get_context(self, token_budget=400):
        return "## User Profile\nName: Alice"


class DummyProjectContext:
    def get_context(self, token_budget=400):
        return "## Current Project Context\n# Ares"


class DummyConversationStore:
    def __init__(self):
        self.exchanges = []

    def add_exchange(self, conversation_id, user_input, assistant_response):
        self.exchanges.append((conversation_id, user_input, assistant_response))


class DummySkill:
    def __init__(self, name, category, description):
        self.name = name
        self.category = category
        self.description = description
        self.content = f"# {name}\n{description}"


class DummySkillManager:
    def __init__(self):
        self.skills = [
            DummySkill("code-review", "coding", "Review code safely"),
            DummySkill("daily-planner", "productivity", "Plan the day"),
        ]

    def list_all(self):
        return self.skills

    def list_categories(self):
        return {"coding": 1, "productivity": 1}

    def search(self, query="", category=""):
        return [
            skill for skill in self.skills
            if query.lower() in skill.name.lower() or query.lower() in skill.description.lower()
        ]

    def get_skill(self, name):
        return next((skill for skill in self.skills if skill.name == name), None)


def make_cli():
    from ares.session import SessionManager
    from ares.sessions import SessionStore
    app = AresCLI.__new__(AresCLI)
    app.console_file = StringIO()
    app.console = Console(file=app.console_file, force_terminal=False, width=120)
    app.config = AppConfig()
    app.agent = DummyAgent()
    app.memory_store = DummyMemoryStore()
    app.soul_manager = DummySoulManager()
    app.profile_manager = DummyProfileManager()
    app.project_context = DummyProjectContext()
    app.conversation_store = DummyConversationStore()
    app.conversation_id = 1
    app.conversation_history = []
    app.icons = {"current": " < current"}
    app.skill_manager = DummySkillManager()
    app.session_manager = SessionManager()
    app.session_store = SessionStore(data_dir=Path(app.config.data_dir).expanduser())
    return app


def test_history_path_is_expanded_and_parent_exists(monkeypatch, tmp_path):
    """Prompt history uses a real home path, not a literal '~' directory."""
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))

    path = _history_path()

    assert "~" not in path
    assert str(tmp_path) in path
    assert (tmp_path).exists()


def test_parse_tool_token_supports_named_and_legacy_formats():
    app = make_cli()

    assert app._parse_tool_token("[tool:web_search:{\"results\": []}]") == (
        "web_search",
        "{\"results\": []}",
    )
    assert app._parse_tool_token("[tool:Stored memory #1: User likes tea]") == (
        "unknown",
        "Stored memory #1: User likes tea",
    )
    assert app._parse_tool_token("[tool:mcp__windows__Snapshot:desktop state]") == (
        "mcp__windows__Snapshot",
        "desktop state",
    )
    assert app._parse_tool_start_token("[tool_start:mcp__windows__Click]") == "mcp__windows__Click"
    assert app._tool_label("mcp__windows__Snapshot") == "Windows: Snapshot"


def test_windows_mcp_activity_uses_a_friendly_label():
    app = make_cli()

    event = app._summarize_tool_result("mcp__windows__Type", "Typed text successfully")

    assert event["label"] == "Windows: Type"
    assert event["detail"] == "Windows type completed"


class FakeMCPManager:
    def __init__(self):
        self.servers = {
            "calendar": MCPServerConfig(
                name="calendar",
                server_url="https://example.com/mcp?token=server-token",
                timeout_seconds=12,
            ),
            "github": MCPServerConfig(
                name="github",
                command="npx",
                args=["server-github"],
                env={"GITHUB_TOKEN": "env-secret"},
            ),
        }
        self.reconnected = []
        self.health_checked = False

    def readiness_report(self):
        return {
            "configured": 2,
            "connected": 1,
            "tools": 3,
            "servers": {
                "calendar": {
                    "ready": True,
                    "transport": "streamable_http",
                    "endpoint": "https://example.com/mcp?token=server-token",
                    "command": "",
                    "tools": 2,
                    "timeout_seconds": 12,
                    "error": "",
                },
                "github": {
                    "ready": False,
                    "transport": "stdio",
                    "endpoint": "",
                    "command": "npx",
                    "tools": 1,
                    "timeout_seconds": 60,
                    "error": "token=error-secret",
                },
            },
        }

    def tools_by_server(self, server_name=None):
        groups = {
            "calendar": [
                {"name": "list_events", "description": "List events"},
                {"name": "create_event", "description": "Create events"},
            ],
            "github": [{"name": "list_issues", "description": "List issues"}],
        }
        if server_name is None:
            return groups
        return {server_name: groups[server_name]} if server_name in groups else {}

    async def reconnect_server(self, name):
        self.reconnected.append(name)
        if name not in self.servers:
            return {"name": name, "ready": False, "error": "not configured", "tools": 0}
        return {
            "name": name,
            "ready": True,
            "transport": self.servers[name].transport,
            "endpoint": self.servers[name].endpoint,
            "command": self.servers[name].command,
            "tools": 2,
            "timeout_seconds": self.servers[name].timeout_seconds,
            "error": "",
        }

    async def health_probe(self):
        self.health_checked = True
        return self.readiness_report()


@pytest.mark.asyncio
async def test_mcp_commands_render_status_tools_reconnect_health_and_redacted_config():
    app = make_cli()
    app.mcp_manager = FakeMCPManager()

    await app._handle_mcp_command("/mcp status")
    await app._handle_mcp_command("/mcp tools calendar")
    await app._handle_mcp_command("/mcp reconnect calendar")
    await app._handle_mcp_command("/mcp health")
    await app._handle_mcp_command("/mcp config")

    output = app.console_file.getvalue()
    assert "MCP Status" in output
    assert "calendar" in output
    assert "list_events" in output
    assert app.mcp_manager.reconnected == ["calendar"]
    assert app.mcp_manager.health_checked is True
    assert "server-token" not in output
    assert "env-secret" not in output
    assert "error-secret" not in output


@pytest.mark.asyncio
async def test_mcp_commands_show_help_and_unknown_server_message():
    app = make_cli()
    app.mcp_manager = FakeMCPManager()

    await app._handle_mcp_command("/mcp")
    await app._handle_mcp_command("/mcp tools missing")

    output = app.console_file.getvalue()
    assert "MCP Commands" in output
    assert "not configured" in output


@pytest.mark.asyncio
async def test_run_coro_works_inside_running_event_loop():
    """The sync entrypoint helper tolerates an already-running event loop."""
    assert _run_coro(_answer()) == 42


def test_model_command_switches_and_saves(monkeypatch):
    app = make_cli()
    saved = []
    monkeypatch.setattr(cli_module, "save_config", saved.append)

    assert app._handle_command("/model mimo-v2.5-free")

    assert app.config.model == "mimo-v2.5-free"
    assert app.agent.model == "mimo-v2.5-free"
    assert saved[0].model == "mimo-v2.5-free"


def test_memory_edit_and_forget_commands():
    app = make_cli()

    assert app._handle_command("/memory edit 12 User likes coffee")
    assert app.memory_store.memories[12]["fact_text"] == "User likes coffee"
    assert app._handle_command("/forget 12")
    assert 12 not in app.memory_store.memories


def test_memory_clean_command_prunes_policy_violations():
    app = make_cli()
    app.memory_store.memories[13] = {
        "fact_id": 13,
        "fact_text": "Delhi weather is rainy tonight",
        "category": "fact",
        "importance": 0.3,
        "confidence": 0.9,
        "created_at": "2026-01-01T00:00:00+00:00",
        "access_count": 0,
        "updated_at": "now",
    }

    assert app._handle_command("/memory clean")

    output = app.console_file.getvalue()
    assert "Memory cleaned" in output
    assert "Policy pruned" in output
    assert "1" in output
    assert 13 not in app.memory_store.memories


def test_model_list_command_renders_table():
    app = make_cli()

    assert app._handle_command("/model list")

    output = app.console_file.getvalue()
    assert "Models" in output
    assert app.config.model in output
    assert "current" in output


def test_skills_commands_render_tables():
    app = make_cli()

    assert app._handle_command("/skills")
    assert app._handle_command("/skills categories")
    assert app._handle_command("/skills search review")

    output = app.console_file.getvalue()
    assert "Skills" in output
    assert "Skill Categories" in output
    assert "Skill Search: review" in output
    assert "code-review" in output


def test_tools_command_renders_mode_table():
    app = make_cli()

    assert app._handle_command("/tools")

    output = app.console_file.getvalue()
    assert "Tool Output" in output
    assert "summary" in output
    assert "active" in output


def test_export_command_calls_exporter(monkeypatch, tmp_path):
    app = make_cli()
    called = {}

    def fake_export_data(**kwargs):
        called.update(kwargs)
        return tmp_path / "out.json"

    monkeypatch.setattr(cli_module, "export_data", fake_export_data)

    assert app._handle_command(f"/export {tmp_path / 'out.json'}")

    assert called["memory_store"] is app.memory_store
    assert called["path"] == str(tmp_path / "out.json")


def test_soul_profile_and_context_commands_render():
    app = make_cli()

    assert app._handle_command("/soul")
    assert app._handle_command("/profile")
    assert app._handle_command("/context")

    output = app.console_file.getvalue()
    assert "Ares Personality" in output or "Soul" in output
    assert "Alice" in output
    assert "Current Project Context" in output


def test_banner_gives_capabilities_and_prompt_ideas():
    app = make_cli()

    app._show_banner()

    output = app.console_file.getvalue()
    assert "Ares" in output
    assert "model" in output
    assert "memory" in output
    assert "Ready." in output
    assert "Try" in output
    assert "review this repo" in output
    assert "🔥" not in output


def test_cleanup_step_reports_sqlite_lock_without_crashing():
    app = make_cli()

    app._cleanup_step(
        "end conversation",
        lambda: (_ for _ in ()).throw(sqlite3.OperationalError("database is locked")),
    )

    output = app.console_file.getvalue()
    assert "Shutdown warning" in output
    assert "database is locked" in output


@pytest.mark.asyncio
async def test_process_input_summarizes_tool_tokens_by_default():
    app = make_cli()
    app.agent = StreamingAgent()

    await app._process_input("search bitcoin")

    output = app.console_file.getvalue()
    assert "Thinking" in output
    assert "Ares working" in output
    assert "Tool | Web Search" in output
    assert "running" in output
    assert "1 result" in output
    assert "Bitcoin is moving today." not in output
    assert "Bitcoin price today" not in output
    assert "Done." in output
    assert "🤖" not in output
    assert app.conversation_history[-1]["content"] == "Done."


@pytest.mark.asyncio
async def test_process_input_does_not_emit_live_status_escape_codes():
    app = make_cli()
    app.agent = StreamingAgent()

    await app._process_input("search bitcoin")

    output = app.console_file.getvalue()
    assert "Tool | Web Search" in output
    assert "\x1b[2K" not in output
    assert "\x1b[0m" not in output
    assert "Thinking" in output


@pytest.mark.asyncio
async def test_process_input_discloses_auto_loaded_skills():
    app = make_cli()
    app.agent = SkillStreamingAgent()

    await app._process_input("control Notepad")

    output = app.console_file.getvalue()
    assert "Skills selected" in output
    assert "computer-use" in output
    assert "Why:" in output
    assert "Ready to work." in output


@pytest.mark.asyncio
async def test_playwright_snapshot_is_collapsed_even_in_details_mode():
    app = make_cli()
    app.agent = MCPStreamingAgent()
    app.tool_output_mode = "details"

    await app._process_input("inspect the current page")

    output = app.console_file.getvalue()
    assert "MCP | Playwright | Snapshot" in output
    assert "240 lines collapsed" in output
    assert "button Save row 239" not in output
    assert "Snapshot reviewed." in output


@pytest.mark.asyncio
async def test_process_input_can_show_tool_details_for_debugging():
    app = make_cli()
    app.agent = StreamingAgent()
    app.tool_output_mode = "details"

    await app._process_input("search bitcoin")

    output = app.console_file.getvalue()
    assert "Web Search" in output
    assert "Bitcoin is moving today." in output
    assert "Bitcoin price today" in output
    assert "Done." in output


def test_tools_command_sets_output_mode():
    app = make_cli()

    assert app._handle_command("/tools hidden")
    assert app.tool_output_mode == "hidden"
    assert app._handle_command("/tools summary")
    assert app.tool_output_mode == "summary"


@pytest.mark.asyncio
async def test_process_input_hides_tool_progress_when_requested():
    app = make_cli()
    app.agent = StreamingAgent()
    app.tool_output_mode = "hidden"

    await app._process_input("search bitcoin")

    output = app.console_file.getvalue()
    assert "Using web search..." not in output
    assert "Tools" not in output
    assert "Done." in output


@pytest.mark.asyncio
async def test_process_input_does_not_leak_generic_tool_content():
    app = make_cli()
    app.agent = MemoryStreamingAgent()

    await app._process_input("remember this")

    output = app.console_file.getvalue()
    assert "memory" in output
    assert "user secret phrase" not in output
    assert "Saved." in output


@pytest.mark.asyncio
async def test_process_input_strips_assistant_emoji():
    app = make_cli()
    app.agent = EmojiStreamingAgent()

    await app._process_input("hello")

    output = app.console_file.getvalue()
    assert "👋" not in output
    assert "😊" not in output
    assert "Hey there What's up?" in output


@pytest.mark.asyncio
async def test_process_input_wraps_assistant_response_with_gutter(monkeypatch):
    app = make_cli()
    app.agent = LongStreamingAgent()
    monkeypatch.setattr(cli_module.shutil, "get_terminal_size", lambda fallback: os.terminal_size((64, 24)))

    await app._process_input("weather")

    output = app.console_file.getvalue()
    assert "Ares" in output
    assert "umbrella handy!" in output
    assert "High:" in output
    assert any(char in output for char in ("╭", "┌", "+"))


@pytest.mark.asyncio
async def test_process_input_renders_markdown_tables_with_rich():
    app = make_cli()
    app.agent = TableStreamingAgent()

    await app._process_input("news")

    output = app.console_file.getvalue()
    assert "Headlines" in output
    assert "Story" in output
    assert "Match update" in output
    assert "| Story | When |" not in output


@pytest.mark.asyncio
async def test_process_input_strips_ansi_colors_and_keeps_long_text(monkeypatch):
    app = make_cli()
    app.agent = ColorLeakStreamingAgent()
    monkeypatch.setattr(cli_module.shutil, "get_terminal_size", lambda fallback: os.terminal_size((58, 24)))

    await app._process_input("colors")

    output = app.console_file.getvalue()
    assert "\x1b[" not in output
    assert "This should not leak cyan" in output
    assert "scoreboard" in output


@pytest.mark.asyncio
async def test_process_input_drops_repeated_opening():
    app = make_cli()
    app.agent = RepeatedOpeningAgent()
    app.conversation_history = [
        {"role": "user", "content": "ahh man you are so dumb"},
        {
            "role": "assistant",
            "content": "Haha okay fair, I deserve that one\n\nLet me fix the actual issue.",
        },
    ]

    await app._process_input("hi who are you")

    output = app.console_file.getvalue()
    assert "deserve that one" not in output
    assert "I'm Ares" in output
    assert "deserve that one" not in app.conversation_history[-1]["content"]


def test_setup_command_runs_onboarding_and_refreshes_agent_model(monkeypatch):
    app = make_cli()
    calls = []

    class DummyWizard:
        def __init__(self, **kwargs):
            assert kwargs["console"] is app.console
            assert kwargs["config"] is app.config
            assert kwargs["profile_manager"] is app.profile_manager
            assert kwargs["soul_manager"] is app.soul_manager

        def run(self, re_run=False):
            calls.append(re_run)
            app.config.model = "mimo-v2.5-free"
            return True

    monkeypatch.setattr(cli_module, "OnboardingWizard", DummyWizard)

    assert app._handle_command("/setup")

    assert calls == [True]
    assert app.agent.model == "mimo-v2.5-free"


def test_cli_clear_current_task_cancellation_uncancels_all(monkeypatch):
    class FakeTask:
        def __init__(self):
            self.count = 3
            self.uncancel_calls = 0

        def cancelling(self):
            return self.count

        def uncancel(self):
            self.uncancel_calls += 1
            self.count -= 1

    task = FakeTask()
    monkeypatch.setattr(cli_module.asyncio, "current_task", lambda: task)

    cli_module._clear_current_task_cancellation()

    assert task.count == 0
    assert task.uncancel_calls == 3
