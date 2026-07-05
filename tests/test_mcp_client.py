"""Tests for MCP client configuration, OAuth storage, and tool routing."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import asyncio

from ares.agent import Agent
from ares.models import AppConfig
from ares.tools.mcp_client import MCPAuthProvider, MCPClientManager, MCPServerConfig


def test_app_config_exposes_mcp_servers_default():
    config = AppConfig()
    # Default includes Playwright, GitHub, and Fetch MCP servers
    names = [s["name"] for s in config.mcp_servers]
    assert "playwright" in names
    assert "github" in names
    assert "fetch" in names


def test_mcp_server_config_defaults():
    config = MCPServerConfig(name="calendar", server_url="https://example.com/mcp")
    assert config.oauth_client_id == ""
    assert config.oauth_scopes == []


def test_auth_provider_stores_expiry_and_detects_expiration(tmp_path):
    auth = MCPAuthProvider(data_dir=str(tmp_path))
    stored = auth._store_token("calendar", {"access_token": "abc", "expires_in": 3600})

    assert (tmp_path / "mcp_tokens" / "calendar.json").exists()
    assert "expires_at" in stored
    assert auth._is_expired(stored) is False

    expired = {
        "access_token": "old",
        "expires_at": (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(),
    }
    assert auth._is_expired(expired) is True


def test_discover_google_oauth_endpoints(tmp_path):
    auth = MCPAuthProvider(data_dir=str(tmp_path))
    endpoints = asyncio.run(
        auth._discover_endpoints("https://calendar.googleapis.com/mcp")
    )

    assert (
        endpoints["authorization_endpoint"]
        == "https://accounts.google.com/o/oauth2/v2/auth"
    )
    assert endpoints["token_endpoint"] == "https://oauth2.googleapis.com/token"


def test_manager_converts_mcp_tool_to_openai_schema():
    manager = MCPClientManager([])
    tool = SimpleNamespace(
        name="list_events",
        description="List calendar events",
        inputSchema={
            "type": "object",
            "properties": {"calendar_id": {"type": "string"}},
        },
    )

    schema = manager._to_openai_schema("calendar", tool)

    assert schema["type"] == "function"
    assert schema["function"]["name"] == "mcp__calendar__list_events"
    assert "[MCP:calendar]" in schema["function"]["description"]
    assert (
        schema["function"]["parameters"]["properties"]["calendar_id"]["type"]
        == "string"
    )


def test_call_tool_rejects_invalid_name():
    manager = MCPClientManager([])
    result = asyncio.run(manager.call_tool("not_mcp", {}))
    assert result.startswith("Error: Invalid MCP tool name")


def test_call_tool_returns_disconnected_error():
    manager = MCPClientManager([])
    result = asyncio.run(manager.call_tool("mcp__calendar__list_events", {}))
    assert "not connected" in result


def test_call_tool_routes_to_session_and_renders_text():
    class FakeSession:
        async def call_tool(self, tool_name, arguments):
            assert tool_name == "list_events"
            assert arguments == {"limit": 2}
            return SimpleNamespace(
                content=[
                    SimpleNamespace(text="event one"),
                    SimpleNamespace(text="event two"),
                ]
            )

    manager = MCPClientManager([])
    manager.sessions["calendar"] = FakeSession()

    result = asyncio.run(manager.call_tool("mcp__calendar__list_events", {"limit": 2}))

    assert result == "event one\nevent two"


def test_agent_refreshes_and_routes_mcp_tools():
    class FakeMCPManager:
        tool_definitions = [
            {
                "type": "function",
                "function": {
                    "name": "mcp__calendar__list_events",
                    "description": "List events",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ]

        async def call_tool(self, tool_name, arguments):
            assert tool_name == "mcp__calendar__list_events"
            assert arguments == {"limit": 1}
            return "mcp result"

    agent = Agent.__new__(Agent)
    agent.mcp_manager = FakeMCPManager()
    agent.tool_executor = None
    agent.refresh_tools()

    assert any(
        tool["function"]["name"] == "mcp__calendar__list_events" for tool in agent.tools
    )

    results = asyncio.run(
        agent.process_tool_calls_async(
            [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {
                        "name": "mcp__calendar__list_events",
                        "arguments": '{"limit": 1}',
                    },
                }
            ]
        )
    )

    assert results[0]["content"] == "mcp result"
    assert results[0]["tool_name"] == "mcp__calendar__list_events"


def test_manager_accepts_mapping_style_mcp_config():
    manager = MCPClientManager({"calendar": {"server_url": "https://example.com/mcp"}})

    assert "calendar" in manager.servers
    assert manager.servers["calendar"].endpoint == "https://example.com/mcp"


def test_manager_accepts_single_key_config_entries():
    manager = MCPClientManager(
        [{"calendar": {"url": "https://example.com/mcp", "timeout_seconds": 12}}]
    )

    assert manager.servers["calendar"].server_url == "https://example.com/mcp"
    assert manager.servers["calendar"].timeout_seconds == 12


def test_stdio_config_infers_transport_from_command():
    config = MCPServerConfig(name="files", command="python", args=["server.py"])

    assert config.transport == "stdio"


def test_call_tool_handles_cancelled_error_without_crashing():
    class FakeSession:
        async def call_tool(self, tool_name, arguments):
            raise asyncio.CancelledError("cancel scope")

    manager = MCPClientManager(
        [{"name": "calendar", "server_url": "https://example.com/mcp"}]
    )
    manager.sessions["calendar"] = FakeSession()

    result = asyncio.run(manager.call_tool("mcp__calendar__list_events", {}))

    assert "was cancelled" in result



def test_mcp_clear_current_task_cancellation_uncancels_all(monkeypatch):
    from ares.tools import mcp_client

    class FakeTask:
        def __init__(self):
            self.count = 2
            self.uncancel_calls = 0

        def cancelling(self):
            return self.count

        def uncancel(self):
            self.uncancel_calls += 1
            self.count -= 1

    task = FakeTask()
    monkeypatch.setattr(mcp_client.asyncio, "current_task", lambda: task)

    mcp_client._clear_current_task_cancellation()

    assert task.count == 0
    assert task.uncancel_calls == 2
