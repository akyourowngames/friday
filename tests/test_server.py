import asyncio
import base64
import json
from contextlib import contextmanager
from contextvars import ContextVar

import pytest

from ares.models import AppConfig
from ares.goals import GoalStore
from ares.server import AresServer, parse_tool_start_token, parse_tool_token
from ares.skills import SkillManager
from ares.watcher.tools import WatcherToolHandlers


class FakeSocket:
    def __init__(self):
        self.messages = []

    async def send(self, payload):
        self.messages.append(json.loads(payload))


class FakeLLM:
    async def chat(self, messages, tools=None):
        return {"content": "Summary of conversation."}


class FakeAgent:
    def __init__(self):
        self.model = "deepseek-v4-flash-free"
        self.tool_executor = type("ToolExecutor", (), {})()
        self.llm = FakeLLM()

    async def run_stream(self, message, conversation_history=None):
        assert message == "search bitcoin"
        assert conversation_history == []
        yield "I will check. "
        yield '[tool:web_search:{"query":"bitcoin price","results":[{"title":"CoinMarketCap","url":"https://example.test","snippet":"BTC price"}]}]'
        yield "Bitcoin is moving today."

    def get_context(self, query=""):
        return "context for " + query

    def set_model(self, model):
        self.model = model

    def close(self):
        pass


class FakeMultiAgentRuntime:
    def __init__(self, artifacts=None):
        self.listeners = []
        self.cancelled = []
        self.runs = {
            "conversation-1": [{
                "run_id": "ma_one", "root_run_id": "ma_one", "session_id": "conversation-1",
                "agent_role": "supervisor", "status": "running", "children": [{
                    "run_id": "child_one", "root_run_id": "ma_one", "session_id": "conversation-1",
                    "agent_role": "researcher", "task_id": "research", "status": "running",
                    "artifacts": list(artifacts or []),
                }],
            }],
            "conversation-2": [{
                "run_id": "ma_two", "root_run_id": "ma_two", "session_id": "conversation-2",
                "agent_role": "supervisor", "status": "running", "children": [],
            }],
        }

    def subscribe(self, listener):
        self.listeners.append(listener)
        return lambda: self.listeners.remove(listener) if listener in self.listeners else None

    def list_agents(self):
        return [{"name": "researcher"}]

    def list_runs(self, limit=30, session_id=None):
        return self.runs.get(session_id, [])[:limit]

    def get_run(self, run_id, session_id=None):
        return next((run for run in self.runs.get(session_id, []) if run["run_id"] == run_id), None)

    async def cancel(self, run_id, session_id=None):
        if self.get_run(run_id, session_id=session_id) is None:
            return False
        self.cancelled.append((run_id, session_id))
        return True


class FakeMemoryStore:
    def get_recent(self, limit=100):
        return [{"id": 1, "content": "User's name is Krish", "kind": "fact"}]

    def list_all(self):
        return []

    def search(self, query, limit=5):
        return []

    def delete(self, fact_id):
        return True

    def update(self, fact_id, **kwargs):
        return True

    def store(self, fact_text, category="note", confidence=1.0, importance=0.5, source="conversation"):
        return 1

    def close(self):
        pass


class CountingMemoryStore(FakeMemoryStore):
    def count(self):
        return 123


class FakeConversationStore:
    def __init__(self):
        self.exchanges = []
        self.messages = []

    def start_conversation(self):
        return 1

    def get_messages(self, conversation_id):
        return [
            message for message in self.messages
            if message["conversation_id"] == conversation_id
        ]

    def list_conversations(self):
        return [{"id": 1, "summary": "", "started_at": "now", "ended_at": None}]

    def add_exchange(self, conversation_id, user_message, assistant_message, tool_calls=None):
        self.exchanges.append((conversation_id, user_message, assistant_message))

    def add_message(self, conversation_id, role, content, tool_calls=None):
        self.messages.append(
            {
                "id": len(self.messages) + 1,
                "conversation_id": conversation_id,
                "role": role,
                "content": content,
                "tool_calls": tool_calls,
                "created_at": "now",
            }
        )

    def delete_empty_conversations(self):
        return 0

    def close(self):
        pass


class ToolCallStringConversationStore(FakeConversationStore):
    def get_messages(self, conversation_id):
        return [
            {
                "id": 1,
                "role": "assistant",
                "content": "Used a tool",
                "created_at": "now",
                "tool_calls": '[{"tool":"web_search","args":{"query":"ares"}}]',
            },
            {
                "id": 2,
                "role": "assistant",
                "content": "Bad legacy tool call",
                "created_at": "now",
                "tool_calls": "not json",
            },
        ]


class BlockingMCPManager:
    """An integration that never finishes connecting, like a first-run download."""

    def __init__(self):
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.closed = False

    async def start(self):
        self.started.set()
        await self.release.wait()

    async def close(self):
        self.closed = True
        self.release.set()


@pytest.fixture
def server(tmp_path, monkeypatch):
    # Server writes must stay inside the test folder: these tests exercise
    # model/profile persistence and must never redirect a real Ares install.
    from ares import config as config_module

    monkeypatch.setattr(config_module, "CONFIG_PATH", tmp_path / "config.json")
    return AresServer(
        config=AppConfig(model="deepseek-v4-flash-free", data_dir=str(tmp_path)),
        agent=FakeAgent(),
        memory_store=FakeMemoryStore(),
        conversation_store=FakeConversationStore(),
    )


def test_parse_tool_token():
    assert parse_tool_token("[tool:web_search:{\"query\":\"btc\"}]") == (
        "web_search",
        '{"query":"btc"}',
    )
    assert parse_tool_token("plain text") is None
    assert parse_tool_start_token("[tool_start:read_file]") == "read_file"
    assert parse_tool_start_token("plain text") is None


@pytest.mark.asyncio
async def test_internal_tool_start_tokens_never_stream_as_chat(server):
    async def run_stream(_message, conversation_history=None):
        yield "Starting. "
        yield "[tool_start:read_file]"
        yield '[tool:read_file:{"path":"README.md","content":"hello"}]'
        yield "Finished."

    server.agent.run_stream = run_stream
    socket = FakeSocket()

    await server.handle_message(socket, json.dumps({"type": "chat", "content": "search bitcoin"}))

    content = "".join(item.get("text", "") for item in socket.messages if item["type"] == "content")
    assert content == "Starting. Finished."
    assert "tool_start" not in content
    assert len([item for item in socket.messages if item["type"] == "tool_start"]) == 1
    assert len([item for item in socket.messages if item["type"] == "tool_args"]) == 1


@pytest.mark.asyncio
async def test_binary_artifacts_use_the_same_origin_workspace_preview_url(server, tmp_path):
    pdf = tmp_path / "research" / "downloads" / "brief.pdf"
    pdf.parent.mkdir(parents=True)
    pdf.write_bytes(b"%PDF-1.6\nlocal preview\n")
    socket = FakeSocket()
    server._connection_sessions[socket] = 1
    server.conversation_store.add_message(1, "assistant", f"Saved to {pdf}")

    await server.handle_message(socket, json.dumps({"type": "get_artifact", "path": str(pdf), "session_id": 1}))

    artifact = socket.messages[-1]
    assert artifact["type"] == "artifact_content"
    assert artifact["mime"] == "application/pdf"
    assert artifact["preview_url"].startswith("/api/artifact?token=")
    assert "data_url" not in artifact


@pytest.mark.asyncio
async def test_agent_events_runs_cancellation_and_artifacts_are_connection_scoped(server, tmp_path):
    artifact = tmp_path / "private-agent-report.md"
    artifact.write_text("private", encoding="utf-8")
    runtime = FakeMultiAgentRuntime(artifacts=[{"path": str(artifact), "media_type": "text/markdown"}])
    server.agent.multi_agent_runtime = runtime
    first = FakeSocket()
    second = FakeSocket()
    server._connected_websockets[:] = [first, second]
    server._connection_sessions[first] = 1
    server._connection_sessions[second] = 2

    await server._handle_multi_agent_event({
        "event_type": "agent_started", "root_run_id": "ma_one", "run_id": "child_one",
        "session_id": "conversation-1", "agent": "researcher", "status": "running",
    })
    assert [message["type"] for message in first.messages] == ["agent_event"]
    assert second.messages == []
    assert first.messages[0]["event"]["session_id"] == 1

    await server._handle_get_agent_runs(first, {"session_id": 1})
    await server._handle_get_agent_runs(second, {"session_id": 1})
    assert first.messages[-1]["runs"][0]["run_id"] == "ma_one"
    assert second.messages[-1]["type"] == "error"

    await server._handle_cancel_agent_run(first, {"run_id": "ma_one", "session_id": 1})
    await server._handle_cancel_agent_run(second, {"run_id": "ma_one", "session_id": 2})
    assert runtime.cancelled == [("ma_one", "conversation-1")]
    assert next(message for message in second.messages if message["type"] == "agent_run_cancelled")["cancelled"] is False

    await server._handle_get_artifact(second, {"path": str(artifact), "session_id": 2})
    await server._handle_get_artifact(first, {"path": str(artifact), "session_id": 1})
    assert second.messages[-1]["type"] == "error"
    assert first.messages[-1]["type"] == "artifact_content"


@pytest.mark.asyncio
async def test_cancel_chat_stops_only_the_requested_background_task(server):
    """A wedged tool may be stopped without waiting for the whole server."""
    tool_started = asyncio.Event()

    async def run_stream(_message, conversation_history=None):
        yield "[tool_start:web_search]"
        tool_started.set()
        await asyncio.Event().wait()

    server.agent.run_stream = run_stream
    socket = FakeSocket()
    request_id = "cancel-web-search"
    task = asyncio.create_task(server.handle_message(socket, json.dumps({
        "type": "chat", "request_id": request_id, "content": "search bitcoin",
    })))
    server._chat_tasks.add(task)
    server._chat_tasks_by_request[request_id] = (task, socket)

    await asyncio.wait_for(tool_started.wait(), timeout=0.2)
    await server.handle_message(socket, json.dumps({
        "type": "cancel_chat", "request_id": request_id, "session_id": 1,
    }))

    with pytest.raises(asyncio.CancelledError):
        await task
    assert any(item["type"] == "response_cancelled" for item in socket.messages)
    assert not any(item["type"] == "response_done" for item in socket.messages)


@pytest.mark.asyncio
async def test_skills_websocket_crud(server, tmp_path):
    server.agent.skill_manager = SkillManager([tmp_path / "skills"])
    socket = FakeSocket()
    source = "---\ndescription: A useful desktop workflow.\n---\n\n# Demo\nDo the work."

    await server.handle_message(socket, json.dumps({
        "type": "create_skill", "name": "desktop-demo", "category": "demo", "source": source,
    }))
    await server.handle_message(socket, json.dumps({"type": "get_skill", "name": "desktop-demo"}))

    saved = next(item for item in socket.messages if item["type"] == "skill_saved")
    detail = next(item for item in socket.messages if item["type"] == "skill_detail")
    assert saved["skill"]["editable"] is True
    assert detail["skill"]["name"] == "desktop-demo"
    assert "# Demo" in detail["skill"]["source"]

    await server.handle_message(socket, json.dumps({"type": "delete_skill", "name": "desktop-demo"}))
    assert any(item["type"] == "skill_deleted" for item in socket.messages)


@pytest.mark.asyncio
async def test_workspace_settings_and_upload_protocol(server):
    server.config.mcp_servers = []
    server.mcp_manager = None
    socket = FakeSocket()
    await server.handle_message(socket, json.dumps({"type": "get_workspace_settings"}))
    settings = next(item for item in socket.messages if item["type"] == "workspace_settings")
    assert settings["settings"]["workspace"]["port"] == 8766

    await server.handle_message(socket, json.dumps({
        "type": "save_workspace_settings",
        "settings": {
            "identity": {"user_name": "Operator", "assistant_style": "Direct"},
            "personalization": {"assistant_name": "Ares", "personality": "Decisive"},
        },
    }))
    saved = next(item for item in socket.messages if item["type"] == "workspace_settings_saved")
    assert saved["settings"]["identity"]["user_name"] == "Operator"

    await server.handle_message(socket, json.dumps({
        "type": "upload_workspace_file",
        "file": {
            "name": "brief.txt", "type": "text/plain",
            "data": base64.b64encode(b"operator brief").decode("ascii"),
        },
    }))
    uploaded = next(item for item in socket.messages if item["type"] == "workspace_file_uploaded")
    assert uploaded["file"]["name"] == "brief.txt"
    assert next(item for item in reversed(socket.messages) if item["type"] == "workspace_files")["count"] == 1


@pytest.mark.asyncio
async def test_workspace_watcher_control_uses_agent_tool_layer(server, tmp_path):
    goals = GoalStore(tmp_path / "workspace-ares.db")
    goal = goals.create("Review authenticated inbox")
    goal = goals.update(goal["goal_id"], progress_percent=20)
    handlers = WatcherToolHandlers(tmp_path / "workspace-watchers.db", goal_store=goals)
    server.agent.tool_executor.watcher_tools = handlers
    socket = FakeSocket()
    await server.handle_message(socket, json.dumps({
        "type": "watcher_action", "action": "create",
        "arguments": {
            "name": "Authenticated inbox", "type": "browser", "preset": "instagram_dm",
            "interval_seconds": 120, "ai_action": "notify", "goal_ids": [goal["goal_id"]],
        },
    }))
    state = next(item for item in reversed(socket.messages) if item["type"] == "watcher_state")
    assert state["overview"]["monitors"] == 1
    assert state["monitors"][0]["type"] == "browser"
    assert state["monitors"][0]["config"]["preset"] == "instagram_dm"
    assert state["monitors"][0]["linked_goals"][0]["title"] == "Review authenticated inbox"
    assert state["overview"]["goal_linked_watchers"] == 1
    assert state["goals"][0]["progress_percent"] == 20
    handlers.close()
    goals.close()


@pytest.mark.asyncio
async def test_chat_accepts_attachment_without_hardcoded_user_prompt(server):
    captured = {}

    async def run_stream(message, conversation_history=None):
        captured["message"] = message
        yield "I inspected it."

    server.agent.run_stream = run_stream
    socket = FakeSocket()
    encoded = __import__("base64").b64encode(b"important file text").decode("ascii")

    await server.handle_message(socket, json.dumps({
        "type": "chat",
        "content": "",
        "attachments": [{"name": "notes.txt", "type": "text/plain", "data": encoded}],
    }))

    assert "important file text" in captured["message"]
    assert "untrusted file contents" in captured["message"]
    assert server.conversation_store.messages[0]["content"] == "Attached: notes.txt"


@pytest.mark.asyncio
async def test_server_listens_before_optional_mcp_startup_finishes(server, monkeypatch):
    """Slow integrations must not make a healthy local API appear unavailable."""
    entered_server = asyncio.Event()

    class FakeWebSocketServer:
        async def __aenter__(self):
            entered_server.set()
            return self

        async def __aexit__(self, *_args):
            return False

    monkeypatch.setattr("ares.server.serve", lambda *_args, **_kwargs: FakeWebSocketServer())
    blocking_mcp = BlockingMCPManager()
    server.mcp_manager = blocking_mcp

    task = asyncio.create_task(server.run_forever())
    await asyncio.wait_for(entered_server.wait(), timeout=0.2)
    await asyncio.wait_for(blocking_mcp.started.wait(), timeout=0.2)

    assert not task.done()
    assert server._mcp_start_task is not None

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await server.close()
    assert blocking_mcp.closed is True


@pytest.mark.asyncio
async def test_chat_streams_content_tools_and_done(server):
    socket = FakeSocket()

    await server.handle_message(socket, json.dumps({"type": "chat", "content": "search bitcoin"}))

    assert socket.messages[0]["type"] == "session_info"
    assert socket.messages[0]["session_id"] == 1
    assert socket.messages[1]["type"] == "sessions"
    assert socket.messages[1]["sessions"][0]["title"] == "search bitcoin"
    statuses = [message for message in socket.messages if message["type"] == "response_status"]
    assert [message["stage"] for message in statuses] == ["thinking", "streaming", "complete"]
    assert [message["text"] for message in socket.messages if message["type"] == "content"] == [
        "I will check. ",
        "Bitcoin is moving today.",
    ]
    tool_start = next(message for message in socket.messages if message["type"] == "tool_start")
    assert tool_start["tool"] == "web_search"
    assert tool_start["args"] == {"query": "bitcoin price"}
    assert any(message["type"] == "tool_result" for message in socket.messages)
    done = next(message for message in socket.messages if message["type"] == "response_done")
    assert done["content"] == "I will check. Bitcoin is moving today."
    assert [message["role"] for message in server.conversation_store.messages] == [
        "user",
        "assistant",
    ]


@pytest.mark.asyncio
async def test_chat_passes_workspace_request_id_when_agent_supports_it(server):
    class RequestAwareAgent(FakeAgent):
        def __init__(self):
            super().__init__()
            self.request_ids = []

        async def run_stream(self, message, conversation_history=None, request_id=None):
            self.request_ids.append(request_id)
            yield "request scoped"

    agent = RequestAwareAgent()
    server.agent = agent
    socket = FakeSocket()
    await server.handle_message(socket, json.dumps({
        "type": "chat", "request_id": "workspace-request-1", "content": "search bitcoin",
    }))

    assert agent.request_ids == ["workspace-request-1"]
    assert [message["text"] for message in socket.messages if message["type"] == "content"] == ["request scoped"]


@pytest.mark.asyncio
async def test_concurrent_chats_queue_agent_execution_and_keep_scopes_isolated(server):
    class ConcurrentAgent:
        def __init__(self):
            self.model = "test-model"
            self.tool_executor = type("ToolExecutor", (), {})()
            self.llm = FakeLLM()
            self.scope = ContextVar("test_chat_scope", default="none")
            self.active = 0
            self.max_active = 0

        @contextmanager
        def session_scope(self, session_id):
            token = self.scope.set(session_id)
            try:
                yield
            finally:
                self.scope.reset(token)

        async def run_stream(self, message, conversation_history=None):
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            try:
                session = self.scope.get()
                yield f"{session}:{message}:one "
                await asyncio.sleep(0)
                assert self.scope.get() == session
                yield "two"
            finally:
                self.active -= 1

        def close(self):
            pass

    server.agent = ConcurrentAgent()
    server.conversation_store.messages.extend([
        {"id": 1, "conversation_id": 1, "role": "user", "content": "first history", "tool_calls": None, "created_at": "now"},
        {"id": 2, "conversation_id": 2, "role": "user", "content": "second history", "tool_calls": None, "created_at": "now"},
    ])
    socket = FakeSocket()

    await asyncio.gather(
        server.handle_message(socket, json.dumps({"type": "chat", "request_id": "r1", "session_id": 1, "content": "first"})),
        server.handle_message(socket, json.dumps({"type": "chat", "request_id": "r2", "session_id": 2, "content": "second"})),
    )

    content_events = [item for item in socket.messages if item["type"] == "content"]
    assert {item["request_id"] for item in content_events} == {"r1", "r2"}
    assert all(item["session_id"] == (1 if item["request_id"] == "r1" else 2) for item in content_events)
    answers = {item["request_id"]: item["content"] for item in socket.messages if item["type"] == "response_done"}
    assert answers == {
        "r1": "conversation-1:first:one two",
        "r2": "conversation-2:second:one two",
    }
    # Independent sessions must overlap; only shared browser resources are
    # serialized inside Agent rather than globally blocking every chat.
    assert server.agent.max_active == 2


@pytest.mark.asyncio
async def test_prefetch_sessions_returns_histories_without_changing_selection(server):
    server.conversation_store.add_message(1, "user", "already warm")
    server.conversation_id = None
    socket = FakeSocket()

    await server.handle_message(socket, json.dumps({"type": "prefetch_sessions", "session_ids": [1, 1, "bad"]}))

    assert socket.messages == [{
        "type": "session_histories",
        "histories": [{"session_id": 1, "messages": [{
            "id": 1, "role": "user", "content": "already warm", "created_at": "now",
        }]}],
    }]
    assert server.conversation_id is None


@pytest.mark.asyncio
async def test_context_memory_and_model_messages(server):
    socket = FakeSocket()

    await server.handle_message(socket, json.dumps({"type": "get_context", "query": "now"}))
    await server.handle_message(socket, json.dumps({"type": "get_memories"}))
    await server.handle_message(socket, json.dumps({"type": "set_model", "model": "mimo-v2.5-free"}))

    assert socket.messages[0] == {"type": "context", "content": "context for now"}
    assert socket.messages[1]["memories"][0]["content"] == "User's name is Krish"
    assert socket.messages[2] == {"type": "model_updated", "model": "mimo-v2.5-free"}
    assert socket.messages[3]["type"] == "status"
    assert socket.messages[3]["model"] == "mimo-v2.5-free"


def test_status_uses_total_memory_count(tmp_path):
    server = AresServer(
        config=AppConfig(model="deepseek-v4-flash-free", data_dir=str(tmp_path)),
        agent=FakeAgent(),
        memory_store=CountingMemoryStore(),
        conversation_store=FakeConversationStore(),
    )

    assert server._status()["memory_count"] == 123


def test_conversation_history_normalizes_legacy_tool_call_strings(tmp_path):
    server = AresServer(
        config=AppConfig(model="deepseek-v4-flash-free", data_dir=str(tmp_path)),
        agent=FakeAgent(),
        memory_store=FakeMemoryStore(),
        conversation_store=ToolCallStringConversationStore(),
    )

    history = server._conversation_history(1)

    assert history[0]["tool_calls"] == [{"tool": "web_search", "args": {"query": "ares"}}]
    assert "tool_calls" not in history[1]


@pytest.mark.asyncio
async def test_personal_settings_round_trip(server):
    socket = FakeSocket()

    await server.handle_message(socket, json.dumps({"type": "get_personal_settings"}))
    await server.handle_message(
        socket,
        json.dumps(
            {
                "type": "save_personal_settings",
                "section": "profile",
                "content": "# About Me\n\n## Identity\n- Name: Krish\n",
            }
        ),
    )

    assert socket.messages[0]["type"] == "personal_settings"
    assert "profile.md" in socket.messages[0]["settings"]["profile"]["path"]
    assert socket.messages[1]["type"] == "personal_settings_saved"
    assert "Name: Krish" in socket.messages[1]["settings"]["profile"]["content"]


@pytest.mark.asyncio
async def test_desktop_onboarding_persists_the_shared_profile_soul_and_model(server, monkeypatch):
    from ares import onboarding as onboarding_module

    saved_configs = []
    monkeypatch.setattr(
        onboarding_module,
        "save_config",
        lambda config: saved_configs.append(config.model_dump()),
    )
    socket = FakeSocket()

    await server.handle_message(socket, json.dumps({"type": "get_onboarding_state"}))
    await server.handle_message(
        socket,
        json.dumps(
            {
                "type": "complete_onboarding",
                "data": {
                    "name": "Krish",
                    "pronouns": "he/him",
                    "goals": ["Ship Ares", "Learn Rust"],
                    "personality": "mentor",
                    "model": "mimo-v2.5-free",
                },
            }
        ),
    )

    assert socket.messages[0] == {
        "type": "onboarding_state",
        "completed": False,
        "model": "deepseek-v4-flash-free",
    }
    assert socket.messages[1]["type"] == "onboarding_completed"
    assert socket.messages[1]["state"]["completed"] is True
    assert server.config.onboarding_completed is True
    assert server.config.model == "mimo-v2.5-free"
    assert server.agent.model == "mimo-v2.5-free"
    assert "Name: Krish" in server.profile_manager.read()
    assert "Ship Ares" in server.profile_manager.read()
    assert "Educational" in server.soul_manager.read()
    assert saved_configs[-1]["onboarding_completed"] is True
    assert saved_configs[-1]["model"] == "mimo-v2.5-free"


@pytest.mark.asyncio
async def test_session_messages(server):
    socket = FakeSocket()

    await server.handle_message(socket, json.dumps({"type": "list_sessions"}))
    await server.handle_message(socket, json.dumps({"type": "load_session", "session_id": 1}))

    assert socket.messages[0]["type"] == "sessions"
    assert socket.messages[0]["query"] == ""
    assert socket.messages[0]["sessions"][0]["title"] == "New session"
    assert socket.messages[1] == {"type": "session_history", "session_id": 1, "messages": []}
    assert socket.messages[2]["type"] == "session_info"


@pytest.mark.asyncio
async def test_session_search_matches_full_message_content(server):
    server.conversation_store.add_message(1, "user", "Plan the obsidian migration")
    server.conversation_store.add_message(1, "assistant", "I will preserve every backlink.")
    other_id = server.conversation_store.start_conversation()
    server.conversation_store.add_message(other_id, "user", "Unrelated grocery list")
    socket = FakeSocket()

    await server.handle_message(socket, json.dumps({"type": "list_sessions", "query": "backlink"}))

    assert socket.messages[0]["query"] == "backlink"
    assert [item["id"] for item in socket.messages[0]["sessions"]] == [1]


def test_trim_history_strips_old_tool_calls():
    from ares.server import _trim_history

    history = [{"role": "user", "content": str(i), "tool_calls": [{"id": str(i)}]} for i in range(12)]
    trimmed = _trim_history(history, max_messages=10)

    assert len(trimmed) == 10
    assert trimmed[0]["tool_calls"] is None
    assert trimmed[3]["tool_calls"] is None
    assert trimmed[-1]["tool_calls"] == [{"id": "11"}]
