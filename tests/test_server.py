import asyncio
import json

import pytest

from ares.models import AppConfig
from ares.server import AresServer, parse_tool_token


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
    # model/profile persistence and must never redirect a real desktop app.
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


@pytest.mark.asyncio
async def test_server_listens_before_optional_mcp_startup_finishes(server, monkeypatch):
    """Slow integrations must not make Electron think the backend is down."""
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
    assert socket.messages[2] == {"type": "content", "text": "I will check. "}
    assert socket.messages[3]["type"] == "tool_start"
    assert socket.messages[3]["tool"] == "web_search"
    assert socket.messages[3]["args"] == {"query": "bitcoin price"}
    assert socket.messages[4]["type"] == "tool_result"
    assert socket.messages[5] == {"type": "content", "text": "Bitcoin is moving today."}
    assert socket.messages[6]["type"] == "response_done"
    assert socket.messages[6]["content"] == "I will check. Bitcoin is moving today."
    assert [message["role"] for message in server.conversation_store.messages] == [
        "user",
        "assistant",
    ]


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
    assert socket.messages[0]["sessions"][0]["title"] == "New session"
    assert socket.messages[1] == {"type": "session_history", "session_id": 1, "messages": []}
    assert socket.messages[2]["type"] == "session_info"


def test_trim_history_strips_old_tool_calls():
    from ares.server import _trim_history

    history = [{"role": "user", "content": str(i), "tool_calls": [{"id": str(i)}]} for i in range(12)]
    trimmed = _trim_history(history, max_messages=10)

    assert len(trimmed) == 10
    assert trimmed[0]["tool_calls"] is None
    assert trimmed[3]["tool_calls"] is None
    assert trimmed[-1]["tool_calls"] == [{"id": "11"}]
