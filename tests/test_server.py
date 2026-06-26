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
        self.tool_executor = type("ToolExecutor", (), {"task_executor": None})()
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


class FakeTaskStore:
    def list_pending(self):
        return [{"id": 1, "title": "Pay rent", "due_text": "in 5 days"}]

    def get_auto_executable(self):
        return []

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

    def add_exchange(self, conversation_id, user_message, assistant_message, tool_calls=None):
        self.exchanges.append((conversation_id, user_message, assistant_message))

    def delete_empty_conversations(self):
        return 0

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


def test_status_counts_pending_tasks_not_completed():
    class CountTaskStore(FakeTaskStore):
        def list_all(self):
            return [
                {"id": 1, "title": "Queued", "status": "pending", "state": "queued"},
                {"id": 2, "title": "Done", "status": "done", "state": "completed"},
                {"id": 3, "title": "Failed", "status": "partial", "state": "failed"},
            ]

        def get_auto_executable(self):
            return [{"id": 1, "title": "Queued", "status": "pending", "state": "queued"}]

    server = AresServer(
        config=AppConfig(model="deepseek-v4-flash-free"),
        agent=FakeAgent(),
        memory_store=FakeMemoryStore(),
        task_store=CountTaskStore(),
        conversation_store=FakeConversationStore(),
    )

    status = server._status()

    assert status["task_count"] == 1
    assert status["total_task_count"] == 3
    assert status["completed_task_count"] == 1
    assert status["auto_exec_count"] == 1


def test_task_event_callback_pushes_debug_event():
    import asyncio

    socket = FakeSocket()
    server = AresServer(
        config=AppConfig(model="deepseek-v4-flash-free"),
        agent=FakeAgent(),
        memory_store=FakeMemoryStore(),
        task_store=FakeTaskStore(),
        conversation_store=FakeConversationStore(),
    )
    server._connected_websockets.append(socket)

    asyncio.run(server._push_task_event_to_clients({
        "task_id": 42,
        "level": "info",
        "step": 1,
        "message": "Starting step",
    }))

    assert socket.messages[0]["type"] == "task_event"
    assert socket.messages[0]["event"]["message"] == "Starting step"
    assert socket.messages[1]["type"] == "status"


def test_notify_auto_complete_uses_llm_composed_chat_message():
    import asyncio

    class NotifyLLM:
        async def chat(self, messages, tools=None):
            return {"content": "Hey Krish — I made the PDF and saved it in reports/out.pdf."}

    class NotifyAgent(FakeAgent):
        def __init__(self):
            super().__init__()
            self.llm = NotifyLLM()

    class NotifyTaskStore(FakeTaskStore):
        def get(self, task_id):
            return {
                "id": task_id,
                "title": "Make a PDF",
                "completion_report": json.dumps({"summary": "PDF created successfully."}),
            }

        def get_artifacts(self, task_id):
            return [{"path": "reports/out.pdf"}]

    socket = FakeSocket()
    server = AresServer(
        config=AppConfig(model="deepseek-v4-flash-free"),
        agent=NotifyAgent(),
        memory_store=FakeMemoryStore(),
        task_store=NotifyTaskStore(),
        conversation_store=FakeConversationStore(),
    )
    server._connected_websockets.append(socket)

    asyncio.run(server._notify_auto_complete({
        "id": 42,
        "title": "Make a PDF",
        "status": "completed",
        "notes": "PDF created successfully.",
    }))

    done = [m for m in socket.messages if m.get("type") == "response_done"][-1]
    assert done["content"] == "Hey Krish — I made the PDF and saved it in reports/out.pdf."
    assert "Background task" not in done["content"]


def test_trim_history_strips_old_tool_calls():
    from ares.server import _trim_history

    history = [{"role": "user", "content": str(i), "tool_calls": [{"id": str(i)}]} for i in range(12)]
    trimmed = _trim_history(history, max_messages=10)

    assert len(trimmed) == 10
    assert trimmed[0]["tool_calls"] is None
    assert trimmed[3]["tool_calls"] is None
    assert trimmed[-1]["tool_calls"] == [{"id": "11"}]
