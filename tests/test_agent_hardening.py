from __future__ import annotations

import json
import asyncio
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from ares.agent import Agent
from ares.memory import MemoryStore
from ares.models import AppConfig, MultiAgentConfig
from ares.multi_agent import (
    AgentExecutionManifest,
    AgentResult,
    AgentRunStatus,
    AgentTeamResult,
    ChildRunManifest,
    ContextMode,
)
from ares.turn_policy import build_turn_execution_context
from ares.multi_agent_resources import BuilderWorktreeManager


class _Registry:
    def snapshot(self):
        return {
            name: SimpleNamespace(name=name)
            for name in ("planner", "researcher", "analyst", "builder", "reviewer", "synthesizer")
        }


class _Runtime:
    def __init__(self, *, latest=None):
        self.registry = _Registry()
        self.latest = latest
        self.delegations = []
        self.resume_calls = []
        self.get_run_calls = []

    def list_agents(self):
        return [{"name": name} for name in self.registry.snapshot()]

    def get_latest_run(self, *, session_id):
        return self.latest

    def get_run(self, run_id, *, session_id):
        self.get_run_calls.append((run_id, session_id))
        if self.latest and run_id in {self.latest.get("run_id"), self.latest.get("root_run_id")}:
            return self.latest
        return None

    async def delegate(self, tasks, **kwargs):
        tasks = tuple(tasks)
        self.delegations.append((tasks, kwargs))
        children = tuple(
            ChildRunManifest(
                run_id=f"child_{task.task_id}",
                task_id=task.task_id,
                role=task.agent,
                session_id=f"agent:ma_test:{task.task_id}:child_{task.task_id}",
                parent_session_id=str(kwargs.get("session_id") or ""),
                parent_run_id="ma_test",
                root_run_id="ma_test",
                status="succeeded",
                dependencies=task.depends_on,
                tools=("web_search",) if task.agent == "researcher" else (),
            )
            for task in tasks
        )
        results = tuple(
            AgentResult(
                task.task_id,
                task.agent,
                AgentRunStatus.SUCCEEDED,
                content=(
                    f"Evidence for {task.task_id}: https://example.com/{task.task_id}"
                    if task.agent == "researcher"
                    else "Synthesis preserves the evidence."
                ),
                run_id=f"child_{task.task_id}",
                root_run_id="ma_test",
                parent_run_id="ma_test",
            )
            for task in tasks
        )
        manifest = AgentExecutionManifest(
            root_run_id="ma_test",
            session_id=str(kwargs.get("session_id") or ""),
            request_id=str(kwargs.get("request_id") or ""),
            child_runs=children,
            execution_waves=(
                tuple(task.task_id for task in tasks if not task.depends_on),
                tuple(task.task_id for task in tasks if task.depends_on),
            ),
            started_at="2026-07-15T00:00:00+00:00",
            completed_at="2026-07-15T00:00:01+00:00",
            status="succeeded",
            duration_seconds=1.0,
        )
        return AgentTeamResult(
            results,
            root_run_id="ma_test",
            execution_waves=manifest.execution_waves,
            manifest=manifest,
        )

    async def resume(self, run_id, **kwargs):
        self.resume_calls.append((run_id, kwargs))
        return await self.delegate((), **kwargs)

    async def close(self):
        return None


def _agent(tmp_path, fake_embedding_provider, *, enabled=True, runtime=None, context_mode=ContextMode.FULL):
    memory = MemoryStore(
        db_path=tmp_path / "memory.db",
        embedding_provider=fake_embedding_provider,
    )
    return Agent(
        memory_store=memory,
        config=AppConfig(
            data_dir=str(tmp_path / "data"),
            project_context_enabled=False,
            multi_agent=MultiAgentConfig(enabled=enabled),
        ),
        session_id="conversation-7",
        multi_agent_runtime=runtime,
        context_mode=context_mode,
    )


@pytest.mark.asyncio
async def test_greeting_exposes_no_tools_and_stale_action_cannot_execute(
    tmp_path, fake_embedding_provider
):
    agent = _agent(tmp_path, fake_embedding_provider, enabled=False)
    calls = 0
    advertised = []

    async def stream(_messages, tools=None):
        nonlocal calls
        advertised.append([item["function"]["name"] for item in tools or []])
        calls += 1
        if calls == 1:
            # A non-compliant provider may still emit a hidden stale tool call.
            yield {"type": "tool_call", "index": 0, "id": "stale", "name": "run_command"}
            yield {
                "type": "tool_call_delta",
                "index": 0,
                "arguments": json.dumps({"command": "start notepad"}),
            }
        else:
            yield {"type": "content", "text": "Hey!"}
        yield {"type": "done"}

    async def forbidden_execute(_name, _arguments):
        raise AssertionError("stale action reached the executor")

    agent.llm.chat_stream = stream
    agent.tool_executor.execute_async = forbidden_execute
    tokens = [
        token
        async for token in agent.run_stream(
            "hey",
            [{"role": "user", "content": "Open Notepad and type a workout routine."}],
        )
    ]

    assert advertised == [[], []]
    assert "Hey!" in tokens
    assert any("current conversation turn does not authorize" in token for token in tokens)
    await agent.close()


def test_greeting_keeps_soul_but_skips_expensive_memory_context(tmp_path, fake_embedding_provider, monkeypatch):
    agent = _agent(tmp_path, fake_embedding_provider, enabled=False)
    agent.soul_manager.write("## Personality\nBe warmly curious.")

    def forbidden_context(**_kwargs):
        raise AssertionError("a greeting must not retrieve semantic memory")

    monkeypatch.setattr("ares.agent.build_user_context", forbidden_context)
    context = build_turn_execution_context("hi", request_id="req-greeting")
    with agent.turn_scope(context):
        rendered = agent.get_context("hi", [])
    assert "Ares Personality" in rendered
    assert "Be warmly curious." in rendered
    asyncio.run(agent.close())


@pytest.mark.asyncio
async def test_real_child_agent_does_not_reenter_delegation_and_edits_isolated_worktree(
    tmp_path, fake_embedding_provider
):
    repository = tmp_path / "repository"
    repository.mkdir()
    for command in (
        ("git", "init", str(repository)),
        ("git", "-C", str(repository), "config", "user.email", "tests@example.test"),
        ("git", "-C", str(repository), "config", "user.name", "Ares Tests"),
    ):
        assert subprocess.run(command, capture_output=True, text=True).returncode == 0
    (repository / "seed.txt").write_text("seed\n", encoding="utf-8")
    assert subprocess.run(("git", "-C", str(repository), "add", "seed.txt"), capture_output=True, text=True).returncode == 0
    assert subprocess.run(("git", "-C", str(repository), "commit", "-m", "seed"), capture_output=True, text=True).returncode == 0
    manager = BuilderWorktreeManager(tmp_path / "worktrees")
    workspace = manager.prepare(repository, root_run_id="ma_child", child_run_id="child")
    assert workspace.isolated

    agent = _agent(tmp_path, fake_embedding_provider, enabled=True)
    agent.delegation_depth = 1
    agent._tool_schema_filter = lambda schemas: [
        schema for schema in schemas if schema["function"]["name"] == "write_file"
    ]
    agent._tool_authorizer = lambda _name, _args: SimpleNamespace(allowed=True)
    agent.refresh_tools()
    target = str(Path(workspace.root) / "implemented.txt")
    calls = 0

    async def stream(_messages, tools=None):
        nonlocal calls
        calls += 1
        assert any(item["function"]["name"] == "write_file" for item in tools or [])
        if calls == 1:
            yield {"type": "tool_call", "index": 0, "id": "edit", "name": "write_file"}
            yield {"type": "tool_call_delta", "index": 0, "arguments": json.dumps({"path": target, "content": "implemented\n"})}
        else:
            yield {"type": "content", "text": "Builder completed the isolated implementation."}
        yield {"type": "done"}

    agent.llm.chat_stream = stream
    try:
        response = "".join([token async for token in agent.run_stream(
            "Have a builder implement a file and a reviewer verify it using multi-agent mode.", []
        )])
        assert "runtime is unavailable" not in response.casefold()
        assert Path(workspace.root, "implemented.txt").read_text(encoding="utf-8") == "implemented\n"
        assert not (repository / "implemented.txt").exists()
    finally:
        await agent.close()
        subprocess.run(("git", "-C", str(repository), "worktree", "remove", "--force", workspace.root), capture_output=True, text=True)


@pytest.mark.asyncio
async def test_cancelling_an_unresponsive_tool_releases_agent_task_and_quarantines_resource(
    tmp_path, fake_embedding_provider
):
    agent = _agent(tmp_path, fake_embedding_provider, enabled=False)
    agent.delegation_depth = 1
    agent.root_run_id = "ma_cancel"
    agent.config.multi_agent.tool_cancel_grace_seconds = 0.1
    started = asyncio.Event()
    release = asyncio.Event()

    async def stubborn_dispatch(_name, _args, _progress):
        started.set()
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            await release.wait()
            return False, "late"

    agent._dispatch_one_tool_async = stubborn_dispatch  # type: ignore[method-assign]
    call = {"function": {"name": "web_search", "arguments": json.dumps({"query": "x"})}}
    pending = asyncio.create_task(agent._execute_one_tool_async(0, call, None))
    await asyncio.wait_for(started.wait(), timeout=1)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(pending, timeout=1)
    assert agent.resource_coordinator.state()["quarantined_operations"]
    release.set()
    await asyncio.sleep(0)
    await agent.close()


@pytest.mark.asyncio
async def test_explicit_separate_researchers_run_real_native_plan_before_synthesis(
    tmp_path, fake_embedding_provider
):
    runtime = _Runtime()
    agent = _agent(tmp_path, fake_embedding_provider, runtime=runtime)
    seen_messages = []

    async def stream(messages, tools=None):
        seen_messages.extend(messages)
        assert tools == []
        yield {"type": "content", "text": "Recommendation based on the verified run."}
        yield {"type": "done"}

    agent.llm.chat_stream = stream
    request = (
        "Research FastAPI, Flask and Django in parallel using separate researchers, "
        "then synthesize a recommendation."
    )
    answer = "".join([token async for token in agent.run_stream(request, [])])

    assert answer.startswith("Recommendation based on the verified run.")
    assert "Agent status:" not in answer
    assert "Verified native run" not in answer
    assert "Execution waves" not in answer
    assert "ma_test" not in answer
    assert "https://example.com/researcher_1" in answer
    assert len(runtime.delegations) == 1
    tasks, kwargs = runtime.delegations[0]
    assert [task.agent for task in tasks[:3]] == ["researcher"] * 3
    assert tasks[-1].agent == "synthesizer"
    assert set(tasks[-1].depends_on) == {task.task_id for task in tasks[:3]}
    assert kwargs["session_id"] == "conversation-7"
    evidence = "\n".join(str(message.get("content") or "") for message in seen_messages)
    assert "Verified Native Agent Execution Evidence" in evidence
    assert '"root_run_id": "ma_test"' in evidence
    assert '"agent_count": 4' in evidence
    assert "https://example.com/researcher_1" in evidence
    await agent.close()


@pytest.mark.asyncio
async def test_telegram_style_with_multiple_agents_uses_native_runtime(
    tmp_path, fake_embedding_provider
):
    runtime = _Runtime()
    agent = _agent(tmp_path, fake_embedding_provider, runtime=runtime)

    async def stream(_messages, tools=None):
        assert tools == []
        yield {"type": "content", "text": "Research summary from the specialist evidence."}
        yield {"type": "done"}

    agent.llm.chat_stream = stream
    answer = "".join([
        token async for token in agent.run_stream(
            "Research on corruption in the world with multiple agents", []
        )
    ])

    assert len(runtime.delegations) == 1
    tasks, _kwargs = runtime.delegations[0]
    assert len(tasks) == 2
    assert all(task.agent == "researcher" for task in tasks)
    assert answer.startswith("Research summary from the specialist evidence.")
    assert "Agent status:" not in answer
    assert "request_id" not in answer
    assert "Execution waves" not in answer
    await agent.close()


def test_execution_guard_removes_unverified_tool_count_claims() -> None:
    cleaned, removed = Agent._strip_unverified_agent_claims(
        "Research timed out.\nZero tool calls made.\nPlease try again."
    )

    assert removed
    assert "tool calls" not in cleaned.casefold()
    assert cleaned == "Research timed out.\nPlease try again."


@pytest.mark.asyncio
async def test_researcher_stops_tools_and_returns_as_soon_as_evidence_exists(
    tmp_path, fake_embedding_provider
):
    agent = _agent(tmp_path, fake_embedding_provider, enabled=False)
    agent.delegation_depth = 1
    agent.specialist_role = "researcher"
    advertised: list[list[str]] = []

    async def execute(name, arguments):
        assert name == "web_search"
        assert arguments == {"query": "corruption evidence"}
        return (
            '{"results": ['
            '{"url": "https://example.com/source-1", "snippet": "evidence one"},'
            '{"url": "https://example.com/source-2", "snippet": "evidence two"}'
            ']}'
        )

    calls = 0

    async def stream(messages, tools=None):
        nonlocal calls
        calls += 1
        advertised.append([item["function"]["name"] for item in tools or []])
        if calls == 1:
            yield {"type": "tool_call", "index": 0, "id": "search", "name": "web_search"}
            yield {
                "type": "tool_call_delta", "index": 0,
                "arguments": json.dumps({"query": "corruption evidence"}),
            }
        else:
            assert tools == []
            assert any(
                "Stop calling tools now" in str(message.get("content") or "")
                for message in messages
            )
            yield {"type": "content", "text": '{"summary":"enough evidence","claims":[],"disagreements":[],"caveats":[]}' }
        yield {"type": "done"}

    agent.tool_executor.execute_async = execute
    agent.llm.chat_stream = stream
    answer = "".join([
        token async for token in agent.run_stream("research corruption", [])
    ])

    assert "web_search" in advertised[0]
    assert advertised[1] == []
    assert "enough evidence" in answer
    assert calls == 2
    await agent.close()


@pytest.mark.asyncio
async def test_all_timed_out_specialists_receive_natural_root_synthesis(
    tmp_path, fake_embedding_provider
):
    runtime = _Runtime()

    async def timed_out_delegate(tasks, **kwargs):
        tasks = tuple(tasks)
        runtime.delegations.append((tasks, kwargs))
        children = tuple(
            ChildRunManifest(
                run_id=f"child_{task.task_id}", task_id=task.task_id, role=task.agent,
                session_id=f"child:{task.task_id}", parent_session_id="conversation-7",
                parent_run_id="ma_timeout", root_run_id="ma_timeout", status="timed_out",
            )
            for task in tasks
        )
        results = tuple(
            AgentResult(
                task.task_id, task.agent, AgentRunStatus.TIMED_OUT,
                error="specialist exceeded its deadline", run_id=f"child_{task.task_id}",
                root_run_id="ma_timeout", parent_run_id="ma_timeout",
            )
            for task in tasks
        )
        manifest = AgentExecutionManifest(
            root_run_id="ma_timeout", session_id="conversation-7",
            request_id=str(kwargs.get("request_id") or ""), child_runs=children,
            execution_waves=(tuple(task.task_id for task in tasks),),
            started_at="2026-07-15T00:00:00+00:00",
            completed_at="2026-07-15T00:05:00+00:00", status="timed_out",
            duration_seconds=300.0,
        )
        return AgentTeamResult(
            results, root_run_id="ma_timeout", execution_waves=manifest.execution_waves,
            manifest=manifest,
        )

    runtime.delegate = timed_out_delegate  # type: ignore[method-assign]
    agent = _agent(tmp_path, fake_embedding_provider, runtime=runtime)

    async def stream(messages, tools=None):
        assert tools == []
        evidence = "\n".join(str(message.get("content") or "") for message in messages)
        assert '"status": "timed_out"' in evidence
        yield {
            "type": "content",
            "text": "The research team hit its deadline before returning usable evidence. Please retry.",
        }
        yield {"type": "done"}

    agent.llm.chat_stream = stream
    answer = "".join([
        token async for token in agent.run_stream(
            "launch multiple agents to research corruption", []
        )
    ])

    assert answer.startswith("The research team hit its deadline")
    assert "Verified native run" not in answer
    assert "Execution waves" not in answer
    assert "ma_timeout" not in answer
    await agent.close()


@pytest.mark.asyncio
async def test_launch_researchers_with_how_much_topic_delegates_before_model_tools(
    tmp_path, fake_embedding_provider
):
    runtime = _Runtime()
    agent = _agent(tmp_path, fake_embedding_provider, runtime=runtime)

    async def stream(_messages, tools=None):
        assert tools == []
        yield {"type": "content", "text": "Research summary from verified child evidence."}
        yield {"type": "done"}

    agent.llm.chat_stream = stream
    answer = "".join([
        token async for token in agent.run_stream(
            "ok launch researchers to research how much corruption is there in world", []
        )
    ])

    assert "meta-questions may only inspect" not in answer
    assert len(runtime.delegations) == 1
    tasks, _kwargs = runtime.delegations[0]
    assert len(tasks) == 2
    assert all(task.agent == "researcher" for task in tasks)
    await agent.close()


@pytest.mark.asyncio
async def test_vague_launch_agents_is_clarified_without_model_or_runtime_call(
    tmp_path, fake_embedding_provider
):
    runtime = _Runtime()
    agent = _agent(tmp_path, fake_embedding_provider, runtime=runtime)

    async def forbidden_stream(*_args, **_kwargs):
        raise AssertionError("vague delegation must not reach the general model")
        yield  # pragma: no cover

    agent.llm.chat_stream = forbidden_stream
    answer = "".join([
        token async for token in agent.run_stream(
            "oh okiee can you laucnh agents multi agent no fluff", []
        )
    ])

    assert "what you want the agents to do" in answer.casefold()
    assert runtime.delegations == []
    await agent.close()


@pytest.mark.asyncio
async def test_explicit_delegation_disabled_fails_honestly_without_model_fallback(
    tmp_path, fake_embedding_provider
):
    agent = _agent(tmp_path, fake_embedding_provider, enabled=False)

    async def forbidden_stream(*_args, **_kwargs):
        raise AssertionError("disabled explicit delegation must not fall through to the model")
        yield  # pragma: no cover

    agent.llm.chat_stream = forbidden_stream
    answer = "".join([
        token async for token in agent.run_stream("Use four agents to compare these frameworks.", [])
    ])

    assert "disabled" in answer.casefold()
    assert "no agents ran" in answer.casefold()
    await agent.close()


@pytest.mark.asyncio
async def test_agent_meta_question_uses_latest_scoped_manifest_and_no_browser(
    tmp_path, fake_embedding_provider
):
    runtime = _Runtime(latest={
        "run_id": "ma_latest",
        "root_run_id": "ma_latest",
        "session_id": "conversation-7",
        "status": "succeeded",
        "metadata": {"execution_waves": [["a", "b"]]},
        "children": [
            {"run_id": "a", "agent_role": "researcher", "status": "succeeded"},
            {"run_id": "b", "agent_role": "reviewer", "status": "succeeded"},
        ],
    })
    agent = _agent(tmp_path, fake_embedding_provider, runtime=runtime)
    captured = []

    async def stream(messages, tools=None):
        captured.extend(messages)
        assert tools == []
        yield {"type": "content", "text": "2 agents ran in ma_latest."}
        yield {"type": "done"}

    agent.llm.chat_stream = stream
    answer = "".join([
        token
        async for token in agent.run_stream(
            "How many agents did you use, and how did you launch them?", []
        )
    ])
    assert captured == []  # meta truth is rendered directly, not delegated to a model
    assert answer == "Agent status: 2 specialists (researcher, reviewer); run succeeded."
    assert "Execution waves" not in answer
    assert "ma_latest" not in answer
    await agent.close()


@pytest.mark.asyncio
async def test_explicit_agent_run_resume_is_direct_session_scoped_runtime_management(
    tmp_path, fake_embedding_provider
):
    runtime = _Runtime()
    agent = _agent(tmp_path, fake_embedding_provider, runtime=runtime)

    async def forbidden_stream(*_args, **_kwargs):
        raise AssertionError("agent-run resume must not fall through to the model")
        yield  # pragma: no cover

    agent.llm.chat_stream = forbidden_stream
    answer = "".join([
        token async for token in agent.run_stream(
            "Resume agent run ma_abcdef1234567890", []
        )
    ])
    assert runtime.resume_calls[0][0] == "ma_abcdef1234567890"
    assert runtime.resume_calls[0][1]["session_id"] == "conversation-7"
    assert "Resumed native agent run ma_abcdef1234567890" in answer
    await agent.close()


@pytest.mark.asyncio
async def test_agent_meta_explicit_run_id_never_falls_back_to_another_session_run(
    tmp_path, fake_embedding_provider
):
    runtime = _Runtime(latest={
        "run_id": "ma_exact", "root_run_id": "ma_exact", "session_id": "conversation-7",
        "status": "succeeded", "children": [],
    })
    agent = _agent(tmp_path, fake_embedding_provider, runtime=runtime)
    answer = "".join([
        token async for token in agent.run_stream("Show agent run ma_missing", [])
    ])
    assert runtime.get_run_calls == [("ma_missing", "conversation-7")]
    assert answer == "Agent run not found in this session."
    await agent.close()


def test_bounded_specialist_context_excludes_personal_and_global_state(
    tmp_path, fake_embedding_provider
):
    agent = _agent(
        tmp_path,
        fake_embedding_provider,
        enabled=False,
        context_mode=ContextMode.BOUNDED_SPECIALIST,
    )
    agent.soul_manager.soul_path.write_text("secret soul", encoding="utf-8")
    agent.profile_manager.profile_path.write_text("secret profile", encoding="utf-8")
    agent.memory_store.store("secret memory", category="fact")

    assert agent.get_context("secret") == ""
    messages = agent.build_messages("bounded task", [], context=agent.get_context("bounded task"))
    rendered = "\n".join(str(message.get("content") or "") for message in messages)
    assert "secret soul" not in rendered
    assert "secret profile" not in rendered
    assert "secret memory" not in rendered
    assert "## Auto-Loaded Skills" not in rendered


def test_bounded_specialist_receives_only_ready_mcp_servers_for_visible_tools(
    tmp_path, fake_embedding_provider
):
    class _Mcp:
        @staticmethod
        def readiness_report():
            return {
                "servers": {
                    "allowed": {"ready": True},
                    "hidden": {"ready": True},
                }
            }

    agent = _agent(
        tmp_path,
        fake_embedding_provider,
        enabled=False,
        context_mode=ContextMode.BOUNDED_SPECIALIST,
    )
    agent.mcp_manager = _Mcp()
    agent.tools = [{"function": {"name": "mcp__allowed__fetch"}}]
    context = agent._live_mcp_context()
    assert "allowed" in context
    assert "hidden" not in context


def test_sync_dispatch_cannot_bypass_current_turn_authority(
    tmp_path, fake_embedding_provider
):
    agent = _agent(tmp_path, fake_embedding_provider, enabled=False)
    called = False

    def forbidden_execute(_name, _arguments):
        nonlocal called
        called = True
        return "unsafe"

    agent.tool_executor.execute = forbidden_execute
    context = build_turn_execution_context("thanks", session_id="conversation-7")
    call = {
        "id": "stale",
        "type": "function",
        "function": {
            "name": "run_command",
            "arguments": json.dumps({"command": "start notepad"}),
        },
    }
    with agent.turn_scope(context):
        result = agent.process_tool_calls([call])

    assert not called
    assert "does not authorize" in result[0]["content"]


@pytest.mark.asyncio
async def test_async_root_dispatch_fails_closed_without_turn_context(
    tmp_path, fake_embedding_provider
):
    agent = _agent(tmp_path, fake_embedding_provider, enabled=False)
    called = False

    async def forbidden_execute(_name, _arguments):
        nonlocal called
        called = True
        return "unsafe"

    agent.tool_executor.execute_async = forbidden_execute
    call = {
        "id": "out-of-band",
        "type": "function",
        "function": {"name": "write_file", "arguments": json.dumps({"path": "unsafe.txt", "content": "x"})},
    }
    result = await agent.process_tool_calls_async([call])

    assert not called
    assert "requires an immutable current-turn" in result[0]["content"]
    await agent.close()
