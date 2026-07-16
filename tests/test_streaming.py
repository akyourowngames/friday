"""Tests for streaming-first tool detection."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from ares.agent import Agent
from ares.latency import LATENCY_EVENTS, LATENCY_METRICS
from ares.llm import LLMClient
from ares.memory import MemoryStore
from ares.models import AppConfig


class FakeStreamResponse:
    def __init__(self, lines, status_code=200):
        self.lines = lines
        self.status_code = status_code

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def raise_for_status(self):
        return None

    async def aiter_lines(self):
        for line in self.lines:
            yield line

    async def aiter_text(self):
        for line in self.lines:
            yield line

    async def aread(self):
        return b"".join(l.encode() for l in self.lines)


class FakeHttpClient:
    def __init__(self, lines):
        self.lines = lines

    def stream(self, *_args, **_kwargs):
        return FakeStreamResponse(self.lines)


class FakeReflectionService:
    def __init__(self):
        self.llm = None
        self.reflector = SimpleNamespace(llm=None)
        self.before_turn_scopes = []
        self.enqueued = []

    async def before_turn(self, scope):
        self.before_turn_scopes.append(scope)

    def enqueue_turn(self, **kwargs):
        self.enqueued.append(kwargs)


@pytest.mark.asyncio
async def test_llm_chat_stream_yields_structured_content_and_tool_chunks():
    lines = [
        'data: {"choices":[{"delta":{"content":"Hi"}}]}',
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_1","function":{"name":"store_memory"}}]}}]}',
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"{\\"content\\":"}}]}}]}',
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"\\"blue\\"}"}}]}}]}',
        "data: [DONE]",
    ]
    client = LLMClient(api_key="test", base_url="http://localhost:1234")
    client._client = FakeHttpClient(lines)

    chunks = [chunk async for chunk in client.chat_stream([{"role": "user", "content": "hi"}], tools=[])]

    assert chunks == [
        {"type": "content", "text": "Hi"},
        {"type": "tool_call", "index": 0, "id": "call_1", "name": "store_memory"},
        {"type": "tool_call_delta", "index": 0, "arguments": '{"content":'},
        {"type": "tool_call_delta", "index": 0, "arguments": '"blue"}'},
        {"type": "done"},
    ]


@pytest.mark.asyncio
async def test_agent_run_stream_no_tools_uses_streaming_only(tmp_path, fake_embedding_provider):
    mem_store = MemoryStore(db_path=tmp_path / "mem.db", embedding_provider=fake_embedding_provider)
    agent = Agent(
        memory_store=mem_store,
        api_key="test-key",
        config=AppConfig(data_dir=str(tmp_path / "ares-data"), project_context_enabled=False),
    )

    async def forbidden_chat(*_args, **_kwargs):
        raise AssertionError("run_stream should not make a blocking preflight chat call")

    async def fake_chat_stream(messages, tools=None):
        assert tools is not None
        for char in "Hello!":
            yield {"type": "content", "text": char}
        yield {"type": "done"}

    agent.llm.chat = forbidden_chat
    agent.llm.chat_stream = fake_chat_stream

    tokens = [token async for token in agent.run_stream("Hi", [])]

    assert "".join(tokens) == "Hello!"


@pytest.mark.asyncio
async def test_agent_run_stream_emits_before_provider_finishes_and_records_latency(
    tmp_path, fake_embedding_provider
):
    mem_store = MemoryStore(db_path=tmp_path / "mem.db", embedding_provider=fake_embedding_provider)
    agent = Agent(
        memory_store=mem_store,
        api_key="test-key",
        session_id="timing-session",
        config=AppConfig(data_dir=str(tmp_path / "ares-data"), project_context_enabled=False),
    )
    first_visible = asyncio.Event()
    release_provider = asyncio.Event()
    provider_finished = asyncio.Event()
    received: list[str] = []

    async def fake_chat_stream(_messages, tools=None):
        yield {"type": "content", "text": "Hello "}
        await release_provider.wait()
        yield {"type": "content", "text": "world!"}
        provider_finished.set()
        yield {"type": "done"}

    async def consume():
        async for token in agent.run_stream("Say hello", [], request_id="stream-timing-1"):
            received.append(token)
            first_visible.set()

    agent.llm.chat_stream = fake_chat_stream
    task = asyncio.create_task(consume())
    await asyncio.wait_for(first_visible.wait(), timeout=0.5)

    # The first provider delta is held only for the small tool-decision grace;
    # a paused provider must not make callers wait for stream completion.
    assert received == ["Hello "]
    assert not provider_finished.is_set()

    await asyncio.sleep(0.01)
    release_provider.set()
    await task

    assert "".join(received) == "Hello world!"
    record = agent.recent_latency_metrics[-1]
    assert record["request_id"] == "stream-timing-1"
    assert record["session_id"] == "timing-session"
    assert set(record["events"]) == set(LATENCY_EVENTS)
    assert set(record["metrics"]) == set(LATENCY_METRICS)
    assert all(value >= 0 for value in record["events"].values())
    assert all(value >= 0 for value in record["metrics"].values())
    assert record["events"]["provider_first_token_received"] <= record["events"]["first_token_sent"]
    assert record["events"]["first_token_sent"] < record["events"]["response_finished"]
    assert (
        record["events"]["first_token_sent"]
        - record["events"]["provider_first_token_received"]
        < 250
    )


@pytest.mark.asyncio
async def test_agent_flushes_deferred_memory_access_stats_after_stream_completion(
    tmp_path, fake_embedding_provider
):
    """Deferred retrieval writes are persisted after, not during, a response."""
    mem_store = MemoryStore(db_path=tmp_path / "mem.db", embedding_provider=fake_embedding_provider)
    fact_id = mem_store.store("The deferred latency counter is persisted after streaming.")
    config = AppConfig(data_dir=str(tmp_path / "ares-data"), project_context_enabled=False)
    config.reflection.enabled = False
    agent = Agent(memory_store=mem_store, api_key="test-key", config=config)
    agent._tools_for_turn = lambda *_args, **_kwargs: []

    async def fake_chat_stream(_messages, tools=None):
        assert tools == []
        yield {"type": "content", "text": "Done."}
        yield {"type": "done"}

    agent.llm.chat_stream = fake_chat_stream
    assert mem_store.get(fact_id)["access_count"] == 0

    tokens = [
        token async for token in agent.run_stream(
            "What do you remember about the deferred latency counter?", []
        )
    ]

    assert "".join(tokens) == "Done."
    assert mem_store.get(fact_id)["access_count"] == 1
    assert mem_store.flush_access_stats() == 0


@pytest.mark.asyncio
async def test_prior_execution_record_does_not_disable_normal_follow_up_streaming(
    tmp_path, fake_embedding_provider,
):
    mem_store = MemoryStore(db_path=tmp_path / "mem.db", embedding_provider=fake_embedding_provider)
    agent = Agent(
        memory_store=mem_store,
        api_key="test-key",
        config=AppConfig(data_dir=str(tmp_path / "ares-data"), project_context_enabled=False),
    )
    # A previous ordinary tool turn is retained for audit/follow-up safety,
    # but it is not evidence about this new request.
    agent._execution_records[agent._execution_session_key(None)] = {
        "request_id": "previous-turn",
        "kind": "ordinary",
    }
    agent._tools_for_turn = lambda *_args, **_kwargs: [{"type": "function"}]
    first_visible = asyncio.Event()
    release_provider = asyncio.Event()
    received: list[str] = []

    async def fake_chat_stream(_messages, tools=None):
        assert tools
        yield {"type": "content", "text": "Fresh "}
        await release_provider.wait()
        yield {"type": "content", "text": "answer."}
        yield {"type": "done"}

    async def consume():
        async for token in agent.run_stream("Give a normal follow-up", []):
            received.append(token)
            first_visible.set()

    agent.llm.chat_stream = fake_chat_stream
    task = asyncio.create_task(consume())
    await asyncio.wait_for(first_visible.wait(), timeout=0.5)
    assert received == ["Fresh "]
    release_provider.set()
    await task
    assert "".join(received) == "Fresh answer."


@pytest.mark.asyncio
async def test_agent_starts_next_chat_while_reflection_is_running(
    tmp_path, fake_embedding_provider,
):
    """A slow prior reflection must not delay the next foreground provider call."""
    mem_store = MemoryStore(db_path=tmp_path / "mem.db", embedding_provider=fake_embedding_provider)
    config = AppConfig(data_dir=str(tmp_path / "ares-data"), project_context_enabled=False)
    config.multi_agent.enabled = False
    agent = Agent(
        memory_store=mem_store,
        api_key="test-key",
        session_id="reflection-stream-session",
        config=config,
    )
    assert agent.reflection_service is not None
    reflection_started = asyncio.Event()
    release_reflection = asyncio.Event()
    response_started = asyncio.Event()

    class SlowReflectionAndChatLLM:
        async def chat(self, _messages, tools=None):
            assert tools == []
            reflection_started.set()
            await release_reflection.wait()
            return {"content": "{}"}

        async def chat_stream(self, _messages, tools=None):
            response_started.set()
            yield {"type": "content", "text": "Foreground reply."}
            yield {"type": "done"}

        async def close(self):
            return None

    llm = SlowReflectionAndChatLLM()
    agent.llm = llm
    agent.reflection_service.llm = llm
    agent.reflection_service.reflector.llm = llm
    agent._tools_for_turn = lambda *_args, **_kwargs: []
    reflection_id = agent.reflection_service.enqueue_turn(
        scope="reflection-stream-session",
        user_text="Remember my dark theme preference.",
        assistant_text="I will remember it.",
    )
    assert reflection_id is not None

    try:
        await asyncio.wait_for(reflection_started.wait(), timeout=0.5)
        task = asyncio.create_task(
            anext(agent.run_stream("Say hello", [], request_id="foreground-during-reflection"))
        )
        await asyncio.wait_for(response_started.wait(), timeout=0.5)
        # The provider is already running while the preceding reflection model
        # call remains deliberately blocked.
        assert agent.reflection_service.store.get(reflection_id)["status"] == "running"
        assert not release_reflection.is_set()
        assert await task == "Foreground reply."
    finally:
        release_reflection.set()
        await agent.close()
        mem_store.close()


@pytest.mark.asyncio
async def test_agent_run_stream_normalizes_proxy_snapshots_and_preserves_final_state(
    tmp_path, fake_embedding_provider
):
    mem_store = MemoryStore(db_path=tmp_path / "mem.db", embedding_provider=fake_embedding_provider)
    agent = Agent(
        memory_store=mem_store,
        api_key="test-key",
        config=AppConfig(data_dir=str(tmp_path / "ares-data"), project_context_enabled=False),
    )
    reflection = FakeReflectionService()
    agent.reflection_service = reflection
    agent._tools_for_turn = lambda *_args, **_kwargs: []

    async def fake_chat_stream(_messages, tools=None):
        assert tools == []
        yield {"type": "content", "text": "Hel"}
        yield {"type": "content", "text": "Hello"}
        yield {"type": "content", "text": "Hello!"}
        yield {"type": "done"}

    agent.llm.chat_stream = fake_chat_stream
    tokens = [token async for token in agent.run_stream("Say hello", [])]

    assert tokens == ["Hel", "lo", "!"]
    assert "".join(tokens) == "Hello!"
    assert agent.last_messages[-1] == {"role": "assistant", "content": "Hello!"}
    assert reflection.enqueued == [{
        "scope": None,
        "user_text": "Say hello",
        "assistant_text": "Hello!",
    }]


@pytest.mark.asyncio
async def test_agent_run_stream_buffers_guard_sensitive_turns_without_duplicate_final_output(
    tmp_path, fake_embedding_provider, monkeypatch
):
    mem_store = MemoryStore(db_path=tmp_path / "mem.db", embedding_provider=fake_embedding_provider)
    agent = Agent(
        memory_store=mem_store,
        api_key="test-key",
        config=AppConfig(data_dir=str(tmp_path / "ares-data"), project_context_enabled=False),
    )
    agent._tools_for_turn = lambda *_args, **_kwargs: []
    monkeypatch.setattr(
        agent,
        "_last_execution_record",
        lambda context: {"kind": "ordinary", "request_id": context.request_id},
    )
    monkeypatch.setattr(agent, "_guard_final_answer", lambda *_args: "Verified final answer.")

    async def fake_chat_stream(_messages, tools=None):
        yield {"type": "content", "text": "Raw "}
        yield {"type": "content", "text": "model answer."}
        yield {"type": "done"}

    agent.llm.chat_stream = fake_chat_stream
    tokens = [token async for token in agent.run_stream("Say hello", [])]

    assert tokens == ["Verified final answer."]
    assert agent.last_messages[-1] == {
        "role": "assistant", "content": "Verified final answer.",
    }


@pytest.mark.asyncio
async def test_agent_run_stream_detects_and_executes_tool_call(tmp_path, fake_embedding_provider):
    mem_store = MemoryStore(db_path=tmp_path / "mem.db", embedding_provider=fake_embedding_provider)
    agent = Agent(
        memory_store=mem_store,
        api_key="test-key",
        config=AppConfig(data_dir=str(tmp_path / "ares-data"), project_context_enabled=False),
    )
    call_count = 0

    async def forbidden_chat(*_args, **_kwargs):
        raise AssertionError("run_stream should not make a blocking preflight chat call")

    async def fake_chat_stream(messages, tools=None):
        nonlocal call_count
        call_count += 1
        assert tools is not None
        if call_count == 1:
            args = json.dumps({"content": "User likes blue", "category": "preference"})
            yield {"type": "tool_call", "index": 0, "id": "call_1", "name": "store_memory"}
            yield {"type": "tool_call_delta", "index": 0, "arguments": args[:20]}
            yield {"type": "tool_call_delta", "index": 0, "arguments": args[20:]}
            yield {"type": "done"}
        else:
            for char in "Stored!":
                yield {"type": "content", "text": char}
            yield {"type": "done"}

    agent.llm.chat = forbidden_chat
    agent.llm.chat_stream = fake_chat_stream

    tokens = [token async for token in agent.run_stream("Remember blue", [])]

    assert "[tool_start:store_memory]" in tokens
    assert any(token.startswith("[tool:store_memory:Stored memory") for token in tokens)
    assert "".join(token for token in tokens if not token.startswith("[tool")) == "Stored!"
    assert mem_store.search("blue")


@pytest.mark.asyncio
async def test_agent_run_stream_suppresses_preamble_before_tool_call(tmp_path, fake_embedding_provider):
    mem_store = MemoryStore(db_path=tmp_path / "mem.db", embedding_provider=fake_embedding_provider)
    agent = Agent(
        memory_store=mem_store,
        api_key="test-key",
        config=AppConfig(data_dir=str(tmp_path / "ares-data"), project_context_enabled=False),
    )
    call_count = 0

    async def fake_chat_stream(messages, tools=None):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            yield {"type": "content", "text": "Today's Thursday, July 9, 2026.\n"}
            yield {"type": "tool_call", "index": 0, "id": "call_1", "name": "read_file"}
            yield {"type": "tool_call_delta", "index": 0, "arguments": json.dumps({"path": "notes.txt"})}
            yield {"type": "done"}
        else:
            yield {"type": "content", "text": "Here is notes.txt."}
            yield {"type": "done"}

    async def fake_execute_async(tool_name, arguments):
        assert tool_name == "read_file"
        return "[File: notes.txt (1 lines total)]\n     1\thello"

    agent.llm.chat_stream = fake_chat_stream
    agent.tool_executor.execute_async = fake_execute_async

    tokens = [token async for token in agent.run_stream("read notes.txt", [])]
    visible = "".join(token for token in tokens if not token.startswith("[tool"))

    assert "Today's Thursday" not in visible
    assert visible == "Here is notes.txt."
    assert agent.last_messages[-1] == {"role": "assistant", "content": "Here is notes.txt."}
