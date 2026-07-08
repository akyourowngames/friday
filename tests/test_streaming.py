"""Tests for streaming-first tool detection."""

import json

import pytest

from ares.agent import Agent
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
