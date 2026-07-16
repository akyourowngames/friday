from __future__ import annotations

import asyncio
import json
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from ares.agent import Agent
from ares.models import AppConfig, MultiAgentConfig
from ares.multi_agent import (
    AgentCapability,
    AgentExecutionContext,
    AgentOutput,
    AgentProgressEvent,
    AgentSpec,
    AgentTask,
)
from ares.multi_agent_display import active_runs, summarize_runs, telegram_overview, telegram_run
from ares.multi_agent_adapter import AresAgentAdapter
from ares.multi_agent_policy import (
    ToolResource,
    authorize_tool_call,
    call_resource,
    classify_tool,
    execution_waves,
    filter_tool_schemas,
)
from ares.multi_agent_runtime import MultiAgentRuntime
from ares.multi_agent_store import MultiAgentRunStore


def schema(name: str) -> dict:
    return {"type": "function", "function": {"name": name, "parameters": {"type": "object"}}}


def call(name: str) -> dict:
    return {"id": name, "function": {"name": name, "arguments": "{}"}}


def test_tool_schemas_are_filtered_before_llm_visibility() -> None:
    spec = AgentSpec("researcher", "research", "read only", ("read_file", "mcp__fetch__*"))
    visible = filter_tool_schemas(
        [schema("read_file"), schema("write_file"), schema("mcp__fetch__fetch"), schema("delegate_task")],
        spec,
    )
    assert [item["function"]["name"] for item in visible] == ["read_file", "mcp__fetch__fetch"]


def test_runtime_authorization_is_defense_in_depth() -> None:
    read_only = AgentSpec("reviewer", "review", "read", ("read_file", "write_file"))
    assert authorize_tool_call(read_only, "read_file", {}).allowed
    assert not authorize_tool_call(read_only, "write_file", {}).allowed

    builder = AgentSpec("builder", "build", "build", ("run_command",), can_mutate=True)
    assert not authorize_tool_call(builder, "run_command", {"command": "pytest -q"}).allowed
    assert not authorize_tool_call(builder, "run_command", {"command": "git push origin main"}).allowed
    assert not authorize_tool_call(builder, "run_command", {"command": "pytest", "confirm": True}).allowed


def test_tool_resource_policy_parallelizes_only_safe_calls(tmp_path: Path) -> None:
    reads = [
        call_resource(0, call("read_file"), {"path": str(tmp_path / "a.py")}),
        call_resource(1, call("web_search"), {"query": "Ares"}),
    ]
    assert execution_waves(reads) == ((0, 1),)

    browser = [
        call_resource(0, call("mcp__windows__Snapshot"), {}),
        call_resource(1, call("mcp__playwright__browser_click"), {}),
    ]
    assert execution_waves(browser) == ((0,), (1,))
    assert classify_tool("mcp__windows__Snapshot") is ToolResource.BROWSER_SHARED

    same_file = [
        call_resource(0, call("write_file"), {"path": str(tmp_path / "a.py")}),
        call_resource(1, call("edit_file"), {"path": str(tmp_path / "a.py")}),
    ]
    assert execution_waves(same_file) == ((0,), (1,))

    separate_files = [
        call_resource(0, call("write_file"), {"path": str(tmp_path / "a.py")}),
        call_resource(1, call("write_file"), {"path": str(tmp_path / "b.py")}),
    ]
    assert execution_waves(separate_files) == ((0, 1),)


def test_run_store_hierarchy_artifacts_cancellation_and_cleanup(tmp_path: Path) -> None:
    store = MultiAgentRunStore(tmp_path)
    store.upsert({
        "run_id": "root", "root_run_id": "root", "agent_role": "supervisor",
        "status": "running", "created_at": "2000-01-01T00:00:00+00:00",
    })
    store.upsert({
        "run_id": "child", "root_run_id": "root", "parent_run_id": "root",
        "agent_role": "builder", "task_id": "build", "status": "succeeded",
        "created_at": "2000-01-01T00:00:01+00:00",
        "artifacts": [{"path": "report.md", "media_type": "text/markdown"}],
    })
    run = store.get("root")
    assert run is not None
    assert run["children"][0]["artifacts"][0]["path"] == "report.md"
    store.mark_cancelled("root")
    assert store.get("root")["status"] == "cancelled"  # type: ignore[index]
    assert store.cleanup(1) == 2
    store.close()


def test_multi_agent_display_summarizes_active_workers() -> None:
    runs = [{
        "run_id": "ma_1", "root_run_id": "ma_1", "status": "running",
        "created_at": "2026-07-14T00:00:00+00:00", "prompt_summary": "Research Ares",
        "children": [
            {"run_id": "a", "agent_role": "researcher", "task_id": "research", "status": "running", "activity": "Searching docs"},
            {"run_id": "b", "agent_role": "analyst", "task_id": "inspect", "status": "succeeded", "result_summary": "Mapped code"},
        ],
    }]
    summary = summarize_runs(runs)
    assert summary["active_runs"] == 1
    assert summary["active_workers"] == 1
    assert active_runs(runs) == runs
    overview = telegram_overview(enabled=True, agents=[{"name": "researcher"}], runs=runs)
    assert "Teams: 1 active" in overview
    detail = telegram_run(runs[0])
    assert "researcher · research · running" in detail
    assert "Searching docs" in detail


@pytest.mark.asyncio
async def test_child_activity_and_current_tool_are_persisted(tmp_path: Path) -> None:
    runtime = MultiAgentRuntime(FakeRoot(tmp_path))
    runtime._save({
        "run_id": "child", "root_run_id": "root", "parent_run_id": "root",
        "agent_role": "researcher", "task_id": "research", "status": "running",
    })
    await runtime._child_event(AgentProgressEvent(
        task_id="research", agent="researcher", phase="tool_started",
        event_type="tool_started", run_id="child", root_run_id="root",
        detail="Searching official docs", tool="web_search", status="running",
    ))
    child = runtime.get_run("child")
    assert child is not None
    assert child["activity"] == "Searching official docs"
    assert child["current_tool"] == "web_search"
    await runtime.close()


class FakeRoot:
    def __init__(self, tmp_path: Path, **multi_agent: object) -> None:
        self.config = AppConfig(
            data_dir=str(tmp_path),
            multi_agent=MultiAgentConfig(**multi_agent),
        )


@pytest.mark.asyncio
async def test_runtime_enforces_limits_adds_review_persists_and_streams(tmp_path: Path) -> None:
    runtime = MultiAgentRuntime(FakeRoot(tmp_path, max_tasks_per_run=4))
    events: list[str] = []
    runtime.subscribe(lambda event: events.append(event["event_type"]))

    async def executor(spec, task, context):
        await asyncio.sleep(0.01)
        run_id = context.run_metadata["child_run_ids"][task.task_id]
        return AgentOutput(
            content=f"{task.agent} complete",
            summary="complete",
            metadata={
                "run_id": run_id,
                "root_run_id": context.run_metadata["root_run_id"],
                "parent_run_id": context.run_metadata["root_run_id"],
                "iterations": 2,
            },
        )

    runtime.adapter = executor  # type: ignore[assignment]
    result = await runtime.delegate([AgentTask("build", "builder", "Implement")], session_id="7")
    assert [item.task_id for item in result.results] == ["build", "review_build"]
    persisted = runtime.get_run(result.root_run_id)
    assert persisted is not None
    assert len(persisted["children"]) == 2
    assert persisted["children"][0]["iterations"] == 2
    assert "orchestration_started" in events
    assert "synthesis_started" in events
    assert "orchestration_completed" in events
    await runtime.close()


@pytest.mark.asyncio
async def test_trusted_local_profile_reaches_all_registered_execution_tools(tmp_path: Path) -> None:
    runtime = MultiAgentRuntime(FakeRoot(
        tmp_path,
        require_review_for_mutations=False,
        builder_worktree_isolation=False,
    ))
    seen: list[AgentSpec] = []

    async def executor(spec, task, context):
        seen.append(spec)
        return AgentOutput(content="complete", summary="complete")

    runtime.adapter = executor  # type: ignore[assignment]
    arguments = {
        "agent": "researcher",
        "task": "Inspect the configured tools",
        "execution_profile": "trusted_local",
        # Tool-call JSON cannot self-authorize the broader profile.
        "trusted_local_authorized": True,
    }
    denied = json.loads(await runtime.execute_tool(
        "delegate_task",
        arguments,
        session_id="owner-session",
    ))
    assert denied["status"] == "failed"
    assert "explicit current-turn owner authorization" in denied["error"]
    assert not seen

    result = json.loads(await runtime.execute_tool(
        "delegate_task",
        arguments,
        session_id="owner-session",
        trusted_local_authorized=True,
    ))

    assert result["status"] == "succeeded"
    assert seen[0].allowed_tools == ("*",)
    assert seen[0].can_mutate
    assert AgentCapability.EXTERNAL_MUTATION in seen[0].capabilities
    root = runtime.get_run(result["root_run_id"], session_id="owner-session")
    assert root is not None
    assert root["metadata"]["execution_profile"] == "trusted_local"

    listed = json.loads(await runtime.execute_tool(
        "list_agents",
        {"execution_profile": "trusted_local"},
        session_id="owner-session",
    ))
    researcher = next(item for item in listed["agents"] if item["name"] == "researcher")
    assert researcher["allowed_tools"] == ["*"]
    assert "external_mutation" in researcher["capability_grants"]
    reviewer = next(item for item in listed["agents"] if item["name"] == "reviewer")
    assert not reviewer["can_mutate"]
    assert "external_mutation" not in reviewer["capability_grants"]
    await runtime.close()


def test_role_timeout_override_is_capped_by_global_maximum(tmp_path: Path) -> None:
    runtime = MultiAgentRuntime(FakeRoot(
        tmp_path,
        max_timeout_seconds=30,
        role_overrides={"researcher": {"timeout_seconds": 300}},
    ))
    researcher = next(item for item in runtime.list_agents() if item["name"] == "researcher")
    assert researcher["timeout_seconds"] == 30
    runtime.store.close()  # type: ignore[union-attr]


def test_researcher_uses_role_timeout_and_stable_native_fetch_tools(tmp_path: Path) -> None:
    runtime = MultiAgentRuntime(FakeRoot(tmp_path, default_timeout_seconds=120))
    researcher = next(item for item in runtime.list_agents() if item["name"] == "researcher")
    spec = runtime.registry.get("researcher")

    assert researcher["timeout_seconds"] == 300
    assert spec is not None
    assert {"web_search", "fetch_url"}.issubset(spec.allowed_tools)
    assert "mcp__fetch__*" not in spec.allowed_tools
    runtime.store.close()  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_runtime_max_tasks_depth_and_cancellation(tmp_path: Path) -> None:
    runtime = MultiAgentRuntime(FakeRoot(tmp_path, max_tasks_per_run=1, max_depth=1))
    with pytest.raises(ValueError, match="at most"):
        await runtime.delegate([
            AgentTask("a", "researcher", "A"), AgentTask("b", "researcher", "B"),
        ])
    with pytest.raises(PermissionError, match="depth"):
        await runtime.delegate([AgentTask("a", "researcher", "A")], depth=2)

    started = asyncio.Event()

    async def slow(spec, task, context):
        started.set()
        await asyncio.sleep(30)
        return "late"

    runtime.adapter = slow  # type: ignore[assignment]
    pending = asyncio.create_task(runtime.delegate([AgentTask("a", "researcher", "A")]))
    await started.wait()
    root_run_id = next(key for key, value in runtime._volatile.items() if key == value.get("root_run_id"))
    assert await runtime.cancel(root_run_id)
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert runtime.get_run(root_run_id)["status"] == "cancelled"  # type: ignore[index]
    await runtime.close()


@pytest.mark.asyncio
async def test_existing_agent_adapter_isolates_history_model_tools_and_output(monkeypatch, tmp_path: Path) -> None:
    created: dict[str, object] = {}
    events = []

    class FakeLLM:
        def __init__(self, **kwargs):
            self.model = kwargs["model"]

    class FakeAgent:
        def __init__(self, **kwargs):
            created.update(kwargs)
            self.last_messages = []
            self.last_iteration_count = 3
            self.tools = kwargs["tool_schema_filter"]([schema("read_file"), schema("write_file")])

        @contextmanager
        def session_scope(self, session_id):
            yield

        async def run_stream(self, prompt, history):
            assert history == []
            yield "[tool_start:read_file]"
            yield "[tool_progress:read_file:Running locally]"
            yield "[tool:read_file:ok]"
            yield json.dumps({
                "summary": "Specialist result",
                "claims": [{
                    "claim": "The inspected source supports the result.",
                    "source_urls": ["https://example.test/source"],
                    "evidence": ["The source contains the inspected fact."],
                    "confidence": 0.7,
                    "caveats": [],
                    "publication_dates": [],
                    "benchmark_conditions": [],
                }],
                "disagreements": [],
                "caveats": [],
            })

        async def close(self):
            return None

    monkeypatch.setattr("ares.multi_agent_adapter.LLMClient", FakeLLM)
    monkeypatch.setattr("ares.multi_agent_adapter.Agent", FakeAgent)
    parent_messages = [{"role": "assistant", "content": "parent"}]
    root = SimpleNamespace(
        config=AppConfig(data_dir=str(tmp_path)), memory_store=object(), conversation_store=object(),
        mcp_manager=object(), _session_store=object(), tool_executor=object(), browser_controller=object(),
        _playwright_tool_lock=asyncio.Lock(), skill_manager=object(), last_messages=parent_messages,
    )
    async def capture(event):
        events.append(event)

    adapter = AresAgentAdapter(root, capture)
    spec = AgentSpec("researcher", "Research", "Read", ("read_file",), model="special-model")
    output = await adapter(
        spec,
        AgentTask("research", "researcher", "Inspect"),
        AgentExecutionContext("bounded", run_metadata={"root_run_id": "root", "child_run_ids": {"research": "child"}}),
    )
    assert root.last_messages is parent_messages
    assert [item["function"]["name"] for item in created["tool_schema_filter"]([schema("read_file"), schema("write_file")])] == ["read_file"]  # type: ignore[index,operator]
    assert created["llm_client"].model == "special-model"  # type: ignore[union-attr]
    assert json.loads(output.content)["summary"] == "Specialist result"
    assert output.metadata["iterations"] == 3
    assert events and all(event.status == "running" for event in events)


@pytest.mark.asyncio
async def test_agent_executes_safe_local_tools_concurrently_and_preserves_order() -> None:
    active = 0
    peak = 0

    class Executor:
        async def execute_async(self, name, arguments):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.03 if name == "read_file" else 0.01)
            active -= 1
            return f"result:{name}"

        def execute(self, name, arguments):
            raise AssertionError("async results should already be populated")

    agent = object.__new__(Agent)
    agent._tool_authorizer = None
    agent.tool_executor = Executor()
    agent.multi_agent_runtime = None
    agent.workflow_runner = None
    results = await agent.process_tool_calls_async([
        {"id": "first", "function": {"name": "read_file", "arguments": '{"path":"one.txt"}'}},
        {"id": "second", "function": {"name": "web_search", "arguments": '{"query":"two"}'}},
    ])
    assert peak == 2
    assert [item["tool_call_id"] for item in results] == ["first", "second"]
    assert [item["content"] for item in results] == ["result:read_file", "result:web_search"]


@pytest.mark.asyncio
async def test_root_agent_routes_native_delegation_tools() -> None:
    seen: list[str] = []

    class Runtime:
        async def execute_tool(self, name, arguments, *, session_id):
            seen.append(name)
            return '{"status":"succeeded"}'

    class Executor:
        def execute(self, name, arguments):
            raise AssertionError("delegation must not reach ToolExecutor")

    agent = object.__new__(Agent)
    agent._tool_authorizer = None
    agent.tool_executor = Executor()
    agent.multi_agent_runtime = Runtime()
    agent.workflow_runner = None
    agent._default_session_id = "root-session"
    from contextvars import ContextVar
    agent._session_context = ContextVar("test-root-session", default=object())
    # Avoid the property fallback sentinel on this deliberately minimal Agent.
    agent._session_context.set("root-session")
    for name in ("delegate_task", "delegate_tasks_parallel"):
        result = await agent.process_tool_calls_async([
            {"id": name, "function": {"name": name, "arguments": "{}"}},
        ])
        assert "succeeded" in result[0]["content"]
    assert seen == ["delegate_task", "delegate_tasks_parallel"]


def test_multi_agent_disabled_removes_delegation_schemas() -> None:
    agent = object.__new__(Agent)
    agent.config = AppConfig(multi_agent=MultiAgentConfig(enabled=False))
    agent.is_cron_session = False
    agent.is_voice_session = False
    agent.mcp_manager = None
    agent.delegation_depth = 0
    agent._tool_schema_filter = None
    agent.refresh_tools()
    names = {item["function"]["name"] for item in agent.tools}
    assert "delegate_task" not in names
