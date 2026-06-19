import json

import pytest

from ares.models import AppConfig
from ares.server import AresServer, parse_tool_token


class FakeSocket:
    def __init__(self):
        self.messages = []

    async def send(self, payload):
        self.messages.append(json.loads(payload))


class FakeAgent:
    def __init__(self):
        self.model = "deepseek-v4-flash-free"

    async def run_stream(self, message, history=None):
        assert message == "search bitcoin"
        assert history == []
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

    def close(self):
        pass


class FakeTaskStore:
    def list_pending(self):
        return [{"id": 1, "title": "Pay rent", "due_text": "in 5 days"}]

    def close(self):
        pass


class FakeConversationStore:
    def __init__(self):
        self.exchanges = []

    def start_conversation(self):
        return 1

    def get_messages(self, conversation_id):
        return []

    def list_conversations(self):
        return [{"id": 1, "summary": "", "started_at": "now", "ended_at": None}]

    def add_exchange(self, conversation_id, user_message, assistant_message):
        self.exchanges.append((conversation_id, user_message, assistant_message))

    def close(self):
        pass


@pytest.fixture
def server():
    return AresServer(
        config=AppConfig(model="deepseek-v4-flash-free"),
        agent=FakeAgent(),
        memory_store=FakeMemoryStore(),
        task_store=FakeTaskStore(),
        conversation_store=FakeConversationStore(),
    )


def test_parse_tool_token():
    assert parse_tool_token("[tool:web_search:{\"query\":\"btc\"}]") == (
        "web_search",
        '{"query":"btc"}',
    )
    assert parse_tool_token("plain text") is None


@pytest.mark.asyncio
async def test_chat_streams_content_tools_and_done(server):
    socket = FakeSocket()

    await server.handle_message(socket, json.dumps({"type": "chat", "content": "search bitcoin"}))

    assert socket.messages[0] == {"type": "content", "text": "I will check. "}
    assert socket.messages[1]["type"] == "tool_start"
    assert socket.messages[1]["tool"] == "web_search"
    assert socket.messages[1]["args"] == {"query": "bitcoin price"}
    assert socket.messages[2]["type"] == "tool_result"
    assert socket.messages[3] == {"type": "content", "text": "Bitcoin is moving today."}
    assert socket.messages[4]["type"] == "response_done"
    assert socket.messages[4]["content"] == "I will check. Bitcoin is moving today."
    assert server.conversation_store.exchanges == [
        (1, "search bitcoin", "I will check. Bitcoin is moving today.")
    ]


@pytest.mark.asyncio
async def test_context_memory_tasks_and_model_messages(server):
    socket = FakeSocket()

    await server.handle_message(socket, json.dumps({"type": "get_context", "query": "now"}))
    await server.handle_message(socket, json.dumps({"type": "get_memories"}))
    await server.handle_message(socket, json.dumps({"type": "get_tasks"}))
    await server.handle_message(socket, json.dumps({"type": "set_model", "model": "mimo-v2.5-free"}))

    assert socket.messages[0] == {"type": "context", "content": "context for now"}
    assert socket.messages[1]["memories"][0]["content"] == "User's name is Krish"
    assert socket.messages[2]["tasks"][0]["title"] == "Pay rent"
    assert socket.messages[3] == {"type": "model_updated", "model": "mimo-v2.5-free"}
    assert socket.messages[4]["type"] == "status"
    assert socket.messages[4]["model"] == "mimo-v2.5-free"


@pytest.mark.asyncio
async def test_session_messages(server):
    socket = FakeSocket()

    await server.handle_message(socket, json.dumps({"type": "list_sessions"}))
    await server.handle_message(socket, json.dumps({"type": "load_session", "session_id": 1}))

    assert socket.messages[0]["type"] == "sessions"
    assert socket.messages[0]["sessions"][0]["title"] == "New session"
    assert socket.messages[1] == {"type": "session_history", "session_id": 1, "messages": []}
    assert socket.messages[2]["type"] == "session_info"
