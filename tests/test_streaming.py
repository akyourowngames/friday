"""Tests for streaming-first tool detection."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from ares.agent import Agent
from ares.infra.latency import LATENCY_EVENTS, LATENCY_METRICS
from ares.integrations.llm import LLMClient
from ares.memory import MemoryStore
from ares.models import AppConfig
from ares.integrations.turn_policy import build_turn_execution_context


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


class FakeChatResponse:
    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text

    def json(self):
        return self._payload


class SequencedChatClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.models = []

    async def post(self, _url, *, json, headers):
        self.models.append(json["model"])
        return self.responses.pop(0)


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


class FakePlaywrightManager:
    def __init__(self):
        self.calls = []
        self.tool_definitions = [
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": "test Playwright tool",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
            for name in (
                "mcp__playwright__browser_navigate",
                "mcp__playwright__browser_snapshot",
            )
        ]

    def readiness_report(self):
        return {"servers": {"playwright": {"ready": True}}}

    async def call_tool(self, tool_name, arguments):
        self.calls.append((tool_name, arguments))
        if tool_name.endswith("browser_navigate"):
            return "Navigated to https://www.instagram.com/"
        if tool_name.endswith("browser_snapshot"):
            return "- document 'Instagram'\n- link 'Home'"
        return "Error: unexpected Playwright tool"


def test_try_again_resolves_the_last_explicit_browser_offer():
    resolved = Agent._resolve_referential_browser_continuation(
        "yeah try again",
        [{
            "role": "assistant",
            "content": "Want me to start a fresh browser session and try again?",
        }],
    )

    assert "Resolved browser continuation" in resolved
    assert build_turn_execution_context(resolved).intent.value == "browser_interaction"


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
async def test_fast_model_failure_falls_back_to_primary_model():
    client = LLMClient(api_key="test", base_url="http://localhost:1234", model="big-pickle")
    transport = SequencedChatClient([
        FakeChatResponse(404, text="fast model unavailable"),
        FakeChatResponse(200, payload={
            "choices": [{"message": {"content": "primary worked"}}]
        }),
    ])
    client._client = transport

    response = await client.chat(
        [{"role": "user", "content": "hello"}],
        tools=[],
        model="deepseek-v4-flash-free",
        fallback_model="big-pickle",
    )

    assert response["content"] == "primary worked"
    assert transport.models == ["deepseek-v4-flash-free", "big-pickle"]


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
    assert record["model"]
    assert record["tool_schema_count"] >= 0
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
        stream = agent.run_stream(
            "Say hello", [], request_id="foreground-during-reflection"
        )
        task = asyncio.create_task(anext(stream))
        await asyncio.wait_for(response_started.wait(), timeout=0.5)
        # The foreground provider is running after the preceding background
        # review was cancelled and durably requeued.
        assert agent.reflection_service.store.get(reflection_id)["status"] == "pending"
        assert not release_reflection.is_set()
        assert await task == "Foreground reply."
        await stream.aclose()
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
    assert len(reflection.enqueued) == 1
    enqueued = reflection.enqueued[0]
    assert enqueued["scope"] is None
    assert enqueued["user_text"] == "Say hello"
    assert enqueued["assistant_text"] == "Hello!"
    outcome = json.loads(enqueued["outcome_summary"])
    assert outcome["tool_outcomes"] == []
    assert outcome["execution_record"]["status"] == "succeeded"


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
async def test_browser_status_only_response_is_retried_as_real_verified_execution(
    tmp_path, fake_embedding_provider
):
    mem_store = MemoryStore(db_path=tmp_path / "mem.db", embedding_provider=fake_embedding_provider)
    manager = FakePlaywrightManager()
    config = AppConfig(data_dir=str(tmp_path / "ares-data"), project_context_enabled=False)
    config.reflection.enabled = False
    agent = Agent(
        memory_store=mem_store,
        api_key="test-key",
        session_id="browser-false-negative",
        config=config,
        mcp_manager=manager,
    )
    response_count = 0

    async def fake_chat_stream(messages, tools=None):
        nonlocal response_count
        response_count += 1
        names = {item["function"]["name"] for item in tools or []}
        assert "mcp__playwright__browser_navigate" in names
        assert "mcp__playwright__browser_snapshot" in names
        if response_count == 1:
            yield {
                "type": "content",
                "text": "Starting a fresh browser session now. Opening Instagram.",
            }
        elif response_count == 2:
            assert "Runtime correction: no Playwright tool executed" in messages[-1]["content"]
            yield {
                "type": "tool_call", "index": 0, "id": "navigate",
                "name": "mcp__playwright__browser_navigate",
            }
            yield {
                "type": "tool_call_delta", "index": 0,
                "arguments": json.dumps({"url": "https://www.instagram.com/"}),
            }
        elif response_count == 3:
            yield {
                "type": "tool_call", "index": 0, "id": "snapshot",
                "name": "mcp__playwright__browser_snapshot",
            }
            yield {"type": "tool_call_delta", "index": 0, "arguments": "{}"}
        else:
            yield {"type": "content", "text": "Instagram is open."}
        yield {"type": "done"}

    agent.llm.chat_stream = fake_chat_stream
    tokens = [
        token async for token in agent.run_stream(
            "yeah new browser sessionn",
            [{"role": "assistant", "content": "I can start a fresh browser and open Instagram."}],
        )
    ]
    visible = "".join(token for token in tokens if not token.startswith("[tool"))

    assert response_count == 4
    assert manager.calls == [
        (
            "mcp__playwright__browser_navigate",
            {"url": "https://www.instagram.com/"},
        ),
        ("mcp__playwright__browser_snapshot", {}),
    ]
    assert visible == "Instagram is open."
    assert "Starting a fresh browser session" not in "".join(tokens)


def test_text_tool_markup_maps_search_watchers_to_advertised_watcher_query():
    calls, cleaned = Agent._text_tool_calls_from_content(
        "Let me check.\n\n<search_watchers>name: HAHA</search_watchers>",
        [{"type": "function", "function": {"name": "list_watchers"}}],
    )

    assert cleaned == "Let me check."
    assert calls == [{
        "id": "text_tool_0",
        "type": "function",
        "function": {"name": "list_watchers", "arguments": '{"query": "HAHA"}'},
    }]


@pytest.mark.asyncio
async def test_agent_run_stream_executes_text_tool_markup_without_showing_it(
    tmp_path, fake_embedding_provider
):
    mem_store = MemoryStore(db_path=tmp_path / "mem.db", embedding_provider=fake_embedding_provider)
    config = AppConfig(data_dir=str(tmp_path / "ares-data"), project_context_enabled=False)
    config.reflection.enabled = False
    agent = Agent(memory_store=mem_store, api_key="test-key", config=config)
    search_files = next(
        tool for tool in agent.tools
        if tool["function"]["name"] == "search_files"
    )
    agent._tools_for_turn = lambda *_args, **_kwargs: [search_files]
    calls = []
    response_count = 0

    async def fake_chat_stream(messages, tools=None):
        nonlocal response_count
        response_count += 1
        assert tools == [search_files]
        if response_count == 1:
            yield {
                "type": "content",
                "text": "Let me check.\n\n<search_files>path: ~/Desktop</search_files>",
            }
        else:
            assert messages[-1]["role"] == "tool"
            yield {"type": "content", "text": "Desktop checked."}
        yield {"type": "done"}

    async def fake_execute_async(tool_name, arguments):
        calls.append((tool_name, arguments))
        return "Desktop files listed."

    agent.llm.chat_stream = fake_chat_stream
    agent.tool_executor.execute_async = fake_execute_async

    tokens = [token async for token in agent.run_stream("show desktop files", [])]
    visible = "".join(token for token in tokens if not token.startswith("[tool"))

    assert calls == [("search_files", {"path": "~/Desktop"})]
    assert visible == "Desktop checked."
    assert "<search_files>" not in "".join(tokens)


@pytest.mark.asyncio
async def test_agent_run_executes_text_tool_markup(tmp_path, fake_embedding_provider):
    mem_store = MemoryStore(db_path=tmp_path / "mem.db", embedding_provider=fake_embedding_provider)
    config = AppConfig(data_dir=str(tmp_path / "ares-data"), project_context_enabled=False)
    config.reflection.enabled = False
    agent = Agent(memory_store=mem_store, api_key="test-key", config=config)
    list_watchers = next(
        tool for tool in agent.tools
        if tool["function"]["name"] == "list_watchers"
    )
    agent._tools_for_turn = lambda *_args, **_kwargs: [list_watchers]
    calls = []
    response_count = 0

    async def fake_chat(messages, tools=None):
        nonlocal response_count
        response_count += 1
        assert tools == [list_watchers]
        if response_count == 1:
            return {"content": "<search_watchers>name: HAHA</search_watchers>"}
        assert messages[-1]["role"] == "tool"
        return {"content": "HAHA has 5 errors."}

    async def fake_execute_async(tool_name, arguments):
        calls.append((tool_name, arguments))
        return "Watcher: HAHA"

    agent.llm.chat = fake_chat
    agent.tool_executor.execute_async = fake_execute_async

    tokens = [token async for token in agent.run("what errors in HAHA", [])]

    assert calls == [("list_watchers", {"query": "HAHA"})]
    assert tokens == ["HAHA has 5 errors."]


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
