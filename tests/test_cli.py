"""Tests for CLI startup helpers."""

import json
import sqlite3
from io import StringIO
from pathlib import Path

import pytest
from rich.console import Console

from ares import cli as cli_module
from ares.__main__ import _run_coro
from ares.cli import AresCLI, _history_path
from ares.models import AppConfig


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
        yield f"[tool:web_search:{json.dumps(payload)}]"
        yield "Done."


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
async def test_process_input_routes_tool_tokens_to_renderers():
    app = make_cli()
    app.agent = StreamingAgent()
    app.icons.update({"thinking": "...", "bot": "Ares"})

    await app._process_input("search bitcoin")

    output = app.console_file.getvalue()
    assert "Web Search" in output
    assert "Bitcoin is moving today." in output
    assert "Bitcoin price today" in output
    assert "Done." in output
    assert app.conversation_history[-1]["content"] == "Done."



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
