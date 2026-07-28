"""Tests for MCP client configuration, OAuth storage, and tool routing."""

from datetime import datetime, timedelta, timezone
from contextvars import ContextVar
from types import SimpleNamespace

import asyncio
import json

import pytest

import ares.tools.mcp_client as mcp_client_module
from ares.agent import Agent
from ares.config import _ensure_mcp_defaults
from ares.models import AppConfig, DEFAULT_MCP_SERVERS
from ares.tools.mcp_client import (
    MCPAuthProvider,
    MCPClientManager,
    MCPServerConfig,
    redact_mcp_text,
)
from ares.integrations.turn_policy import (
    ActionGrantUseRegistry,
    build_turn_execution_context,
)


def test_app_config_exposes_mcp_servers_default():
    config = AppConfig()
    # Default includes browser, integration, fetch, and Windows desktop MCP servers.
    names = [s["name"] for s in config.mcp_servers]
    assert "playwright" in names
    assert "github" in names
    assert "fetch" in names
    assert "windows" in names


def test_windows_mcp_is_restricted_to_desktop_interaction_tools():
    config = next(server for server in DEFAULT_MCP_SERVERS if server["name"] == "windows")

    assert config["command"] == "uvx"
    assert config["args"][:2] == ["windows-mcp", "serve"]
    allow_list = config["args"][config["args"].index("--tools") + 1]
    assert "PowerShell" not in allow_list
    assert "Registry" not in allow_list
    assert "FileSystem" not in allow_list
    assert "MultiEdit" in allow_list
    assert "MultiSelect" in allow_list


def test_existing_builtin_windows_mcp_gets_snapshot_compatibility_env():
    config = AppConfig()
    windows = next(server for server in config.mcp_servers if server["name"] == "windows")
    windows["env"] = {"WINDOWS_MCP_DISABLE_FLASH": "true"}

    _ensure_mcp_defaults(config)

    assert windows["env"]["ARES_WINDOWS_MCP_COMPAT"] == "1"
    assert windows["env"]["PYTHONPATH"]


def test_existing_builtin_windows_mcp_gains_safe_batch_tools():
    config = AppConfig()
    windows = next(server for server in config.mcp_servers if server["name"] == "windows")
    tools_index = windows["args"].index("--tools") + 1
    windows["args"][tools_index] = windows["args"][tools_index].replace(
        ",MultiEdit,MultiSelect",
        "",
    )

    _ensure_mcp_defaults(config)

    allow_list = windows["args"][tools_index].split(",")
    assert allow_list.count("MultiEdit") == 1
    assert allow_list.count("MultiSelect") == 1


def test_windows_mcp_compat_replaces_lone_surrogates_in_text_output():
    from ares.infra.windows_mcp_compat import _sanitize_result

    result = _sanitize_result(["safe", "broken \ud83d", {"nested": "still \ud83d"}])

    assert result == ["safe", "broken ?", {"nested": "still ?"}]


def test_mcp_server_config_defaults():
    config = MCPServerConfig(name="calendar", server_url="https://example.com/mcp")
    assert config.oauth_client_id == ""
    assert config.oauth_scopes == []
    assert config.timeout_seconds == 60
    assert config.max_timeout_seconds == 600


def test_mcp_server_config_validates_finite_default_and_max_timeouts():
    config = MCPServerConfig(
        name="calendar",
        server_url="https://example.com/mcp",
        timeout_seconds=12,
        max_timeout_seconds=45,
    )

    assert config.timeout_seconds == 12
    assert config.max_timeout_seconds == 45
    with pytest.raises(ValueError, match="timeout_seconds cannot exceed"):
        MCPServerConfig(name="calendar", timeout_seconds=46, max_timeout_seconds=45)
    with pytest.raises(ValueError, match="must be finite"):
        MCPServerConfig(name="calendar", max_timeout_seconds=float("inf"))


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


def test_playwright_and_windows_tool_schemas_explain_routing():
    manager = MCPClientManager([])
    browser = SimpleNamespace(name="browser_snapshot", description="Read the accessibility tree", inputSchema={})
    desktop = SimpleNamespace(name="Snapshot", description="Read a desktop window", inputSchema={})

    playwright_schema = manager._to_openai_schema("playwright", browser)
    windows_schema = manager._to_openai_schema("windows", desktop)

    assert "Preferred for browser and web-page automation" in playwright_schema["function"]["description"]
    assert "before Windows MCP" in playwright_schema["function"]["description"]
    assert "native Windows desktop apps" in windows_schema["function"]["description"]
    assert "Do not use for normal websites" in windows_schema["function"]["description"]


def test_windows_action_schema_requires_local_semantic_target_metadata():
    manager = MCPClientManager([])
    desktop_type = SimpleNamespace(
        name="Type",
        description="Type into a desktop control",
        inputSchema={
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "loc": {"type": "array"},
            },
            "required": ["text"],
        },
    )

    schema = manager._to_openai_schema("windows", desktop_type)
    parameters = schema["function"]["parameters"]
    semantic = parameters["properties"]["__ares"]

    assert "__ares" in parameters["required"]
    assert "expected_region" in semantic["required"]
    assert "text_owner" in semantic["required"]
    assert "ui_generation" in semantic["required"]
    assert "validated locally" in semantic["description"]
    # Discovery must not mutate the server-owned schema object.
    assert "__ares" not in desktop_type.inputSchema["properties"]


def test_windows_semantic_metadata_is_not_sent_to_mcp_server():
    class FakeSession:
        async def call_tool(self, tool_name, arguments):
            assert tool_name == "Type"
            assert arguments == {"text": "hello", "loc": [10, 20]}
            return SimpleNamespace(content=[SimpleNamespace(text="typed")])

    manager = MCPClientManager([])
    manager.sessions["windows"] = FakeSession()

    result = asyncio.run(manager.call_tool(
        "mcp__windows__Type",
        {
            "text": "hello",
            "loc": [10, 20],
            "__ares": {
                "expected_app": "notepad",
                "expected_region": "editor",
                "purpose": "write note",
                "semantic_intent": "type_note",
                "phase": "type_message",
                "text_owner": "editor",
                "ui_generation": 2,
            },
        },
    ))

    assert result == "typed"


def test_windows_bootstrap_launch_schema_does_not_invent_a_ui_generation():
    manager = MCPClientManager([])
    launch = SimpleNamespace(
        name="Launch",
        description="Launch an app",
        inputSchema={
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
    )

    schema = manager._to_openai_schema("windows", launch)
    semantic = schema["function"]["parameters"]["properties"]["__ares"]

    assert "__ares" in schema["function"]["parameters"]["required"]
    assert "expected_region" in semantic["required"]
    assert "ui_generation" not in semantic["required"]
    assert "bootstrap Launch" in schema["function"]["description"]


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


def test_windows_snapshot_uses_short_timeout_and_recovers_once(monkeypatch):
    class HungSession:
        async def call_tool(self, tool_name, arguments):
            await asyncio.sleep(3600)

    manager = MCPClientManager(
        [{"name": "windows", "command": "windows-mcp", "timeout_seconds": 90}]
    )
    manager.sessions["windows"] = HungSession()
    recoveries: list[str] = []

    async def fake_recover(reason):
        recoveries.append(reason)

    async def immediate_timeout(awaitable, timeout):
        awaitable.close()
        raise asyncio.TimeoutError

    monkeypatch.setattr(manager, "_recover_windows_server", fake_recover)
    monkeypatch.setattr("ares.tools.mcp_client.asyncio.wait_for", immediate_timeout)

    result = asyncio.run(manager.call_tool("mcp__windows__Snapshot", {}))

    assert "timed out after 15s" in result
    assert len(recoveries) == 1


def test_readiness_report_uses_schema_cache():
    manager = MCPClientManager([{"name": "calendar", "server_url": "https://example.com/mcp"}])
    manager.schema_cache["calendar"] = [{"function": {"name": "mcp__calendar__list_events"}}]
    manager.server_errors["calendar"] = "offline"

    report = manager.readiness_report()

    assert report["configured"] == 1
    assert report["connected"] == 0
    assert report["servers"]["calendar"]["schema_cached"] is True
    assert report["servers"]["calendar"]["error"] == "offline"
    assert report["servers"]["calendar"]["max_timeout_seconds"] == 600
    assert report["health"]["status"] == "offline"
    assert report["health"]["servers"][0]["name"] == "calendar"


def test_readiness_report_is_sorted_and_redacts_diagnostics():
    manager = MCPClientManager(
        [
            {"name": "zeta", "server_url": "https://user:password@example.com/mcp?token=top-secret"},
            {"name": "alpha", "command": "python", "args": ["server.py"]},
        ]
    )
    manager.server_errors["zeta"] = "Authorization: Bearer top-secret"

    report = manager.readiness_report()

    assert list(report["servers"]) == ["alpha", "zeta"]
    assert report["errors"] == {"zeta": "Authorization: Bearer [redacted]"}
    assert "top-secret" not in report["servers"]["zeta"]["endpoint"]
    assert "password" not in report["servers"]["zeta"]["endpoint"]


def test_redact_mcp_text_hides_query_and_flag_secrets():
    text = "https://example.com/mcp?api_key=abc&access_token=def&project=ares --token xyz"

    redacted = redact_mcp_text(text)

    assert "abc" not in redacted
    assert "def" not in redacted
    assert "xyz" not in redacted
    assert "project=ares" in redacted


def test_tools_by_server_groups_and_sorts_discovered_tools():
    manager = MCPClientManager(
        [
            {"name": "calendar", "server_url": "https://example.com/calendar"},
            {"name": "github", "server_url": "https://example.com/github"},
        ]
    )
    manager.schema_cache = {
        "calendar": [
            {"function": {"name": "mcp__calendar__list_events", "description": "[MCP:calendar] List events"}},
            {"function": {"name": "mcp__calendar__create_event", "description": "[MCP:calendar] Create an event"}},
        ],
        "github": [
            {"function": {"name": "mcp__github__list_issues", "description": "[MCP:github] List issues"}},
        ],
    }

    groups = manager.tools_by_server()

    assert list(groups) == ["calendar", "github"]
    assert [tool["name"] for tool in groups["calendar"]] == ["create_event", "list_events"]
    assert groups["github"][0]["description"] == "List issues"
    assert list(manager.tools_by_server("github")) == ["github"]


def test_reconnect_server_reports_success(monkeypatch):
    async def fake_connect(self, name, config):
        self.sessions[name] = object()
        self.schema_cache[name] = [{"function": {"name": f"mcp__{name}__tool"}}]
        self.tool_definitions.extend(self.schema_cache[name])

    manager = MCPClientManager([{"name": "calendar", "server_url": "https://example.com/mcp"}])
    monkeypatch.setattr(MCPClientManager, "_connect_server", fake_connect)

    report = asyncio.run(manager.reconnect_server("calendar"))

    assert report["ready"] is True
    assert report["tools"] == 1


@pytest.mark.asyncio
async def test_transport_is_opened_and_closed_by_the_same_owner_task(monkeypatch):
    task_events: list[tuple[str, str, asyncio.Task[object] | None]] = []

    class TaskAffineContext:
        def __init__(self, label, value):
            self.label = label
            self.value = value
            self.owner = None

        async def __aenter__(self):
            self.owner = asyncio.current_task()
            task_events.append((self.label, "enter", self.owner))
            return self.value

        async def __aexit__(self, exc_type, exc, traceback):
            current = asyncio.current_task()
            task_events.append((self.label, "exit", current))
            assert current is self.owner

    class FakeSession(TaskAffineContext):
        def __init__(self, read_stream, write_stream):
            super().__init__("session", self)

        async def initialize(self):
            return None

        async def list_tools(self):
            return SimpleNamespace(tools=[])

    monkeypatch.setattr(
        mcp_client_module,
        "stdio_client",
        lambda params: TaskAffineContext("transport", (object(), object())),
    )
    monkeypatch.setattr(
        mcp_client_module,
        "StdioServerParameters",
        lambda **kwargs: SimpleNamespace(**kwargs),
    )
    monkeypatch.setattr(mcp_client_module, "ClientSession", FakeSession)
    manager = MCPClientManager([{"name": "local", "command": "fake-mcp"}])

    await manager._connect_server("local", manager.servers["local"])
    owner = manager._owner_tasks["local"]
    assert owner is not asyncio.current_task()
    await manager.close_server("local")

    assert [(label, action) for label, action, _ in task_events] == [
        ("transport", "enter"),
        ("session", "enter"),
        ("session", "exit"),
        ("transport", "exit"),
    ]
    assert {task for _, _, task in task_events} == {owner}


def test_manager_auto_reconnects_after_initial_connection_failure(monkeypatch):
    attempts: list[str] = []

    async def flaky_connect(self, name, config):
        attempts.append(name)
        if len(attempts) == 1:
            raise ConnectionError("server is still starting")
        self.sessions[name] = object()
        self.schema_cache[name] = [{"function": {"name": f"mcp__{name}__tool"}}]
        self.tool_definitions.extend(self.schema_cache[name])

    manager = MCPClientManager(
        [{"name": "calendar", "server_url": "https://example.com/mcp"}],
        reconnect_interval_seconds=0.01,
    )
    monkeypatch.setattr(MCPClientManager, "_connect_server", flaky_connect)

    async def exercise():
        await manager.start()
        for _ in range(50):
            if manager.readiness_report()["servers"]["calendar"]["ready"]:
                break
            await asyncio.sleep(0.01)
        assert manager.readiness_report()["servers"]["calendar"]["ready"] is True
        await manager.close()

    asyncio.run(exercise())

    assert attempts == ["calendar", "calendar"]


def test_transport_failure_is_evicted_and_reconnected_without_replaying_call(monkeypatch):
    calls: list[str] = []
    connections: list[str] = []

    class DeadSession:
        async def call_tool(self, tool_name, arguments):
            calls.append(tool_name)
            raise ConnectionError("transport closed")

    class HealthySession:
        async def call_tool(self, tool_name, arguments):
            calls.append(tool_name)
            return SimpleNamespace(content=[SimpleNamespace(text="recovered")])

    async def healthy_connect(self, name, config):
        connections.append(name)
        self.sessions[name] = HealthySession()

    manager = MCPClientManager(
        [{"name": "calendar", "server_url": "https://example.com/mcp"}]
    )
    manager.sessions["calendar"] = DeadSession()
    monkeypatch.setattr(MCPClientManager, "_connect_server", healthy_connect)

    async def exercise():
        failed = await manager.call_tool("mcp__calendar__create_event", {"title": "demo"})
        assert "transport closed" in failed
        assert "connection recovered" in failed.casefold()
        assert "calendar" in manager.sessions

        recovered = await manager.call_tool("mcp__calendar__list_events", {})
        assert recovered == "recovered"

    asyncio.run(exercise())

    assert calls == ["create_event", "list_events"]
    assert connections == ["calendar"]


def test_read_only_call_reconnects_and_retries_once_after_transport_drop(monkeypatch):
    calls: list[str] = []
    connections: list[str] = []
    schema = {"function": {"name": "mcp__calendar__list_events"}}

    class DeadSession:
        async def call_tool(self, tool_name, arguments):
            calls.append(tool_name)
            raise ConnectionError("transport closed")

    class HealthySession:
        async def call_tool(self, tool_name, arguments):
            calls.append(tool_name)
            return SimpleNamespace(content=[SimpleNamespace(text="recovered read")])

    async def healthy_connect(self, name, config):
        connections.append(name)
        self.sessions[name] = HealthySession()
        self.tool_definitions = [schema]

    manager = MCPClientManager(
        [{"name": "calendar", "server_url": "https://example.com/mcp"}]
    )
    manager.sessions["calendar"] = DeadSession()
    manager.schema_cache["calendar"] = [schema]
    manager.tool_definitions = [schema]
    monkeypatch.setattr(MCPClientManager, "_connect_server", healthy_connect)

    result = asyncio.run(manager.call_tool("mcp__calendar__list_events", {}))

    assert result == "recovered read"
    assert calls == ["list_events", "list_events"]
    assert connections == ["calendar"]
    assert manager.tool_definitions == [schema]


def test_disconnected_cached_schemas_are_not_advertised_to_the_model() -> None:
    schema = {"function": {"name": "mcp__calendar__list_events"}}
    manager = MCPClientManager(
        [{"name": "calendar", "server_url": "https://example.com/mcp"}]
    )
    manager.sessions["calendar"] = object()
    manager.schema_cache["calendar"] = [schema]
    manager.tool_definitions = [schema]

    asyncio.run(manager.close_server("calendar"))

    assert manager.schema_cache["calendar"] == [schema]
    assert manager.tool_definitions == []
    assert manager.readiness_report()["servers"]["calendar"]["schema_cached"] is True


def test_targeted_recovery_uses_circuit_breaker_without_touching_other_servers(
    monkeypatch,
) -> None:
    attempts: list[str] = []

    async def failed_connect(self, name, config):
        attempts.append(name)
        raise ConnectionError("offline")

    manager = MCPClientManager(
        [
            {"name": "playwright", "command": "fake-browser"},
            {"name": "windows", "command": "fake-desktop"},
        ],
        reconnect_interval_seconds=30,
    )
    monkeypatch.setattr(MCPClientManager, "_connect_server", failed_connect)

    async def exercise():
        first = await manager.ensure_server_running("windows")
        second = await manager.ensure_server_running("windows")
        await manager.close()
        return first, second

    first, second = asyncio.run(exercise())

    assert attempts == ["windows"]
    assert not first["ready"]
    assert second["retry_after_seconds"] > 0


def test_maintenance_probes_a_dead_session_and_reconnects_in_the_same_pass(monkeypatch):
    connections: list[str] = []

    class DeadSession:
        async def list_tools(self):
            raise ConnectionError("heartbeat found a dead transport")

    class HealthySession:
        async def list_tools(self):
            return SimpleNamespace(tools=[])

    async def healthy_connect(self, name, config):
        connections.append(name)
        self.sessions[name] = HealthySession()

    manager = MCPClientManager(
        [{"name": "calendar", "server_url": "https://example.com/mcp"}],
        reconnect_interval_seconds=0.01,
        health_probe_interval_seconds=0.01,
    )
    manager.sessions["calendar"] = DeadSession()
    manager._last_health_probe_at = -1.0
    monkeypatch.setattr(MCPClientManager, "_connect_server", healthy_connect)

    report = asyncio.run(manager.maintain_connections_once())

    assert connections == ["calendar"]
    assert report["servers"]["calendar"]["ready"] is True


def test_reconnect_monitor_survives_one_maintenance_exception(monkeypatch):
    attempts = 0
    recovered = asyncio.Event()
    manager = MCPClientManager(
        [{"name": "calendar", "server_url": "https://example.com/mcp"}],
        reconnect_interval_seconds=0.01,
    )

    async def flaky_maintenance():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("one broken maintenance pass")
        recovered.set()
        return manager.readiness_report()

    monkeypatch.setattr(manager, "maintain_connections_once", flaky_maintenance)

    async def exercise():
        manager._start_reconnect_monitor()
        await asyncio.wait_for(recovered.wait(), timeout=0.5)
        await manager.close()

    asyncio.run(exercise())

    assert attempts >= 2


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
    agent._turn_context = ContextVar("test_mcp_turn")
    agent._turn_grant_uses = ActionGrantUseRegistry()
    agent.delegation_depth = 1
    agent.refresh_tools()

    assert any(
        tool["function"]["name"] == "mcp__calendar__list_events" for tool in agent.tools
    )

    with agent.turn_scope(build_turn_execution_context("List calendar events")):
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


def test_call_tool_strips_reserved_metadata_caps_timeout_and_returns_structured_envelope():
    received: list[dict[str, object]] = []

    class FakeSession:
        async def call_tool(self, tool_name, arguments):
            assert tool_name == "list_events"
            received.append(arguments)
            return SimpleNamespace(content=[SimpleNamespace(text="event one")])

    manager = MCPClientManager(
        [
            {
                "name": "calendar",
                "server_url": "https://example.com/mcp",
                "timeout_seconds": 7,
                "max_timeout_seconds": 7,
            }
        ]
    )
    manager.sessions["calendar"] = FakeSession()

    raw = asyncio.run(
        manager.call_tool(
            "mcp__calendar__list_events",
            {
                "limit": 2,
                "__ares": {
                    "timeout_seconds": 999,
                    "response_format": "structured",
                },
            },
        )
    )
    response = json.loads(raw)

    assert received == [{"limit": 2}]
    assert set(response) == {
        "ok",
        "status",
        "summary",
        "data",
        "artifacts",
        "warnings",
        "errors",
        "next_actions",
        "provenance",
        "metrics",
        "undo_id",
    }
    assert response["ok"] is True
    assert response["data"]["result"] == "event one"
    assert response["metrics"]["timeout_seconds"] == 7
    assert response["provenance"] == {"server": "calendar", "tool": "list_events"}


def test_call_tool_honors_per_call_timeout_up_to_configured_maximum():
    received: list[dict[str, object]] = []

    class FakeSession:
        async def call_tool(self, tool_name, arguments):
            received.append(arguments)
            return SimpleNamespace(content=[SimpleNamespace(text="event one")])

    manager = MCPClientManager(
        [
            {
                "name": "calendar",
                "server_url": "https://example.com/mcp",
                "timeout_seconds": 7,
                "max_timeout_seconds": 45,
            }
        ]
    )
    manager.sessions["calendar"] = FakeSession()

    requested = json.loads(
        asyncio.run(
            manager.call_tool(
                "mcp__calendar__list_events",
                {"__ares": {"timeout_seconds": 30, "response_format": "structured"}},
            )
        )
    )
    capped = json.loads(
        asyncio.run(
            manager.call_tool(
                "mcp__calendar__list_events",
                {"__ares": {"timeout_seconds": 999, "response_format": "structured"}},
            )
        )
    )

    assert received == [{}, {}]
    assert requested["metrics"]["timeout_seconds"] == 30
    assert capped["metrics"]["timeout_seconds"] == 45


def test_operation_timeout_for_matches_call_policy_without_connecting():
    manager = MCPClientManager(
        [
            {
                "name": "calendar",
                "server_url": "https://example.com/mcp",
                "timeout_seconds": 7,
                "max_timeout_seconds": 45,
            },
            {
                "name": "windows",
                "command": "windows-mcp",
                "timeout_seconds": 90,
                "max_timeout_seconds": 90,
            },
        ]
    )

    assert manager.operation_timeout_for(
        "mcp__calendar__list_events", {"__ares": {"timeout_seconds": 30}}
    ) == 30
    assert manager.operation_timeout_for(
        "mcp__calendar__list_events", {"__ares": {"timeout_seconds": 999}}
    ) == 45
    assert manager.operation_timeout_for("mcp__windows__Snapshot", {}) == 15
    assert manager.sessions == {}
    assert manager._exit_stacks == {}


def test_windows_snapshot_keeps_short_default_but_can_use_configured_maximum():
    class FakeSession:
        async def call_tool(self, tool_name, arguments):
            return SimpleNamespace(content=[SimpleNamespace(text="desktop")])

    manager = MCPClientManager(
        [
            {
                "name": "windows",
                "command": "windows-mcp",
                "timeout_seconds": 90,
                "max_timeout_seconds": 90,
            }
        ]
    )
    manager.sessions["windows"] = FakeSession()

    default = json.loads(
        asyncio.run(
            manager.call_tool(
                "mcp__windows__Snapshot",
                {"__ares": {"response_format": "structured"}},
            )
        )
    )
    extended = json.loads(
        asyncio.run(
            manager.call_tool(
                "mcp__windows__Snapshot",
                {"__ares": {"timeout_seconds": 75, "response_format": "structured"}},
            )
        )
    )

    assert default["metrics"]["timeout_seconds"] == 15
    assert extended["metrics"]["timeout_seconds"] == 75


def test_call_tool_reconnects_configured_server_once_before_first_call(monkeypatch):
    connections: list[str] = []
    closed: list[str] = []
    calls: list[tuple[str, dict[str, object]]] = []

    class FakeSession:
        async def call_tool(self, tool_name, arguments):
            calls.append((tool_name, arguments))
            return SimpleNamespace(content=[SimpleNamespace(text="event one")])

    async def fake_connect(self, name, config):
        connections.append(name)
        self.sessions[name] = FakeSession()

    async def fake_close(self, name):
        closed.append(name)

    manager = MCPClientManager(
        [{"name": "calendar", "server_url": "https://example.com/mcp"}]
    )
    monkeypatch.setattr(MCPClientManager, "_connect_server", fake_connect)
    monkeypatch.setattr(MCPClientManager, "close_server", fake_close)

    result = asyncio.run(manager.call_tool("mcp__calendar__list_events", {"limit": 1}))

    assert result == "event one"
    assert closed == ["calendar"]
    assert connections == ["calendar"]
    assert calls == [("list_events", {"limit": 1})]


def test_call_tool_never_replays_started_playwright_mutation_after_reconnect(monkeypatch):
    connections: list[str] = []
    calls: list[tuple[str, dict[str, object]]] = []

    class FailingSession:
        async def call_tool(self, tool_name, arguments):
            calls.append((tool_name, arguments))
            raise ConnectionError("connection reset after request started")

    async def fake_connect(self, name, config):
        connections.append(name)
        self.sessions[name] = FailingSession()

    manager = MCPClientManager(
        [{"name": "playwright", "server_url": "https://example.com/mcp"}]
    )
    monkeypatch.setattr(MCPClientManager, "_connect_server", fake_connect)

    result = asyncio.run(
        manager.call_tool("mcp__playwright__browser_click", {"selector": "#submit"})
    )

    assert "connection reset after request started" in result
    assert connections == ["playwright", "playwright"]
    assert calls == [("browser_click", {"selector": "#submit"})]


def test_call_tool_caches_only_explicit_read_only_operations():
    calls: list[tuple[str, dict[str, object]]] = []

    class FakeSession:
        async def call_tool(self, tool_name, arguments):
            calls.append((tool_name, arguments))
            return SimpleNamespace(content=[SimpleNamespace(text=f"result {len(calls)}")])

    manager = MCPClientManager(
        [{"name": "calendar", "server_url": "https://example.com/mcp"}]
    )
    manager.sessions["calendar"] = FakeSession()

    read_arguments = {"limit": 2, "__ares_cache_ttl_seconds": 60}
    first = asyncio.run(manager.call_tool("mcp__calendar__list_events", read_arguments))
    second = asyncio.run(manager.call_tool("mcp__calendar__list_events", read_arguments))
    assert first == second == "result 1"
    assert calls == [("list_events", {"limit": 2})]

    write_arguments = {"title": "standup", "__ares_cache_ttl_seconds": 60}
    third = asyncio.run(manager.call_tool("mcp__calendar__create_event", write_arguments))
    fourth = asyncio.run(manager.call_tool("mcp__calendar__create_event", write_arguments))
    assert third == "result 2"
    assert fourth == "result 3"
    assert calls[1:] == [
        ("create_event", {"title": "standup"}),
        ("create_event", {"title": "standup"}),
    ]


def test_call_tool_paginates_read_only_structured_responses_with_bounded_requests():
    calls: list[dict[str, object]] = []

    class FakeSession:
        async def call_tool(self, tool_name, arguments):
            assert tool_name == "list_events"
            calls.append(arguments)
            page = arguments["page"]
            return SimpleNamespace(
                structured_content={
                    "items": [{"id": page}],
                    "has_more": page < 2,
                }
            )

    manager = MCPClientManager(
        [{"name": "calendar", "server_url": "https://example.com/mcp"}]
    )
    manager.sessions["calendar"] = FakeSession()

    raw = asyncio.run(
        manager.call_tool(
            "mcp__calendar__list_events",
            {
                "calendar_id": "primary",
                "__ares": {
                    "pagination": {"max_pages": 4},
                    "response_format": "structured",
                },
            },
        )
    )
    response = json.loads(raw)

    assert calls == [
        {"calendar_id": "primary", "page": 1},
        {"calendar_id": "primary", "page": 2},
    ]
    pagination = response["data"]["pagination"]
    assert [item["id"] for item in pagination["items"]] == [1, 2]
    assert pagination["page_count"] == 2
    assert response["metrics"]["pagination"] == {"requested": True, "applied": True}


def test_call_tool_structured_errors_are_redacted_before_returning_to_callers():
    class FailingSession:
        async def call_tool(self, tool_name, arguments):
            return SimpleNamespace(
                isError=True,
                content=[SimpleNamespace(text="Authorization: Bearer secret-value token=also-secret")],
            )

    manager = MCPClientManager(
        [{"name": "calendar", "server_url": "https://example.com/mcp"}]
    )
    manager.sessions["calendar"] = FailingSession()

    raw = asyncio.run(
        manager.call_tool(
            "mcp__calendar__list_events",
            {"__ares_response_format": "structured"},
        )
    )
    response = json.loads(raw)

    assert response["ok"] is False
    assert "secret-value" not in raw
    assert "also-secret" not in raw
    assert "[redacted]" in response["errors"][0]["diagnostic"]
