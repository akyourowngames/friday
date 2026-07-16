from __future__ import annotations

import asyncio
from contextlib import contextmanager
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from ares.models import AppConfig, MultiAgentConfig
from ares.multi_agent import (
    AgentCapability,
    AgentExecutionContext,
    AgentResult,
    AgentOutput,
    AgentRunStatus,
    AgentSpec,
    AgentTask,
    ContextMode,
    MultiAgentOrchestrator,
    RetryableAgentError,
    AgentExecutionManifest,
    ChildRunManifest,
    trusted_local_agent_spec,
)
from ares.multi_agent_adapter import AresAgentAdapter
from ares.multi_agent_policy import (
    ActionGrantRegistry,
    ToolResource,
    authorize_tool_call,
    call_resource,
    classify_tool,
    execution_waves,
    filter_tool_schemas,
)
from ares.multi_agent_research import (
    ResearchClaim,
    parse_research_claims,
    synthesis_confidence,
    validate_research_claim,
)
from ares.multi_agent_resources import BuilderWorktreeManager, ResourceCoordinator
from ares.multi_agent_runtime import MultiAgentRuntime
from ares.multi_agent_store import MultiAgentRunStore


def _call(name: str) -> dict:
    return {"function": {"name": name}}


def test_execution_waves_preserve_read_mutate_read_order_and_unknown_writes(tmp_path: Path) -> None:
    path = str(tmp_path / "state.txt")
    resources = [
        call_resource(0, _call("read_file"), {"path": path}),
        call_resource(1, _call("write_file"), {"path": path}),
        call_resource(2, _call("read_file"), {"path": path}),
    ]
    assert execution_waves(resources) == ((0,), (1,), (2,))

    unknown = [
        call_resource(0, _call("write_file"), {}),
        call_resource(1, _call("write_file"), {}),
    ]
    assert execution_waves(unknown) == ((0,), (1,))
    assert classify_tool("resume_agent_run") is ToolResource.DELEGATION


def test_execution_manifest_is_deeply_immutable_and_serializes_detached_metadata() -> None:
    source = {"nested": {"count": 1}, "items": [{"name": "original"}]}
    child = ChildRunManifest(
        run_id="child",
        task_id="task",
        role="analyst",
        session_id="agent:root:task:child",
        parent_session_id="conversation-1",
        parent_run_id="root",
        root_run_id="root",
        status="succeeded",
        metadata=source,
    )
    manifest = AgentExecutionManifest(
        root_run_id="root",
        session_id="conversation-1",
        request_id="request-1",
        child_runs=(child,),
        execution_waves=(("task",),),
        started_at="2026-07-14T00:00:00+00:00",
        completed_at="2026-07-14T00:00:01+00:00",
        status="succeeded",
        metadata=source,
    )
    source["nested"]["count"] = 999
    source["items"][0]["name"] = "mutated"
    with pytest.raises(TypeError):
        manifest.metadata["nested"]["count"] = 2  # type: ignore[index]
    payload = manifest.as_dict()
    assert payload["metadata"]["nested"]["count"] == 1
    assert payload["child_runs"][0]["metadata"]["items"][0]["name"] == "original"
    payload["metadata"]["nested"]["count"] = 3
    assert manifest.as_dict()["metadata"]["nested"]["count"] == 1


def test_browser_observation_and_interaction_have_distinct_capabilities() -> None:
    observer = AgentSpec(
        "observer", "observe", "read",
        ("mcp__playwright__browser_snapshot", "mcp__playwright__browser_click"),
        capabilities=(AgentCapability.BROWSER_READ,),
    )
    assert authorize_tool_call(observer, "mcp__playwright__browser_snapshot", {}).allowed
    denied = authorize_tool_call(
        observer, "mcp__playwright__browser_click", {"ref": "a"}
    )
    assert not denied.allowed
    assert "browser_interaction" in denied.reason


@pytest.mark.parametrize(
    "name",
    (
        "mcp__slack__chat_postMessage",
        "mcp__notion__update_page",
        "mcp__drive__upload_file",
        "mcp__cms__edit_entry",
    ),
)
def test_unknown_or_mutating_mcp_operations_fail_closed(name: str) -> None:
    spec = AgentSpec(
        "reader",
        "read connected data",
        "never mutate",
        ("mcp__*",),
        capabilities=(AgentCapability.FILESYSTEM_READ,),
    )
    decision = authorize_tool_call(spec, name, {"value": "unsafe"})
    assert not decision.allowed
    assert "external_mutation" in decision.reason


def test_semantically_read_only_mcp_operation_remains_available() -> None:
    spec = AgentSpec(
        "reader",
        "read connected data",
        "never mutate",
        ("mcp__calendar__list_events",),
        capabilities=(AgentCapability.FILESYSTEM_READ,),
    )
    assert authorize_tool_call(spec, "mcp__calendar__list_events", {}).allowed


def test_trusted_local_profile_broadens_tool_visibility_but_keeps_dispatch_guards(
    tmp_path: Path,
) -> None:
    profile = trusted_local_agent_spec(AgentSpec(
        "researcher", "research", "read only", ("web_search",),
    ))
    schemas = [
        {"type": "function", "function": {"name": name, "parameters": {}}}
        for name in (
            "read_file",
            "write_file",
            "run_command",
            "mcp__custom__perform_unusual_operation",
            "delegate_task",
        )
    ]
    visible = filter_tool_schemas(schemas, profile)
    assert [item["function"]["name"] for item in visible] == [
        "read_file",
        "write_file",
        "run_command",
        "mcp__custom__perform_unusual_operation",
    ]
    assert profile.can_mutate
    assert profile.permits_capability(AgentCapability.EXTERNAL_MUTATION)
    assert not profile.permits_capability(AgentCapability.DELEGATION)
    assert profile.permits_tool("mcp__custom__perform_unusual_operation")

    reviewer = trusted_local_agent_spec(AgentSpec(
        "reviewer", "review", "read only", ("read_file",),
        capabilities=(AgentCapability.FILESYSTEM_READ,),
    ))
    assert reviewer.allowed_tools == ("read_file",)
    assert not reviewer.can_mutate
    assert not reviewer.permits_capability(AgentCapability.FILESYSTEM_WRITE)

    no_grant = authorize_tool_call(
        profile,
        "mcp__custom__perform_unusual_operation",
        {},
    )
    assert not no_grant.allowed
    assert "action grant" in no_grant.reason

    confirmation = authorize_tool_call(
        profile,
        "mcp__custom__perform_unusual_operation",
        {"confirm": True},
    )
    assert not confirmation.allowed
    assert "cannot originate user confirmation" in confirmation.reason

    escaped_path = authorize_tool_call(
        profile,
        "write_file",
        {"path": str(tmp_path.parent / "outside.txt")},
        workspace_root=str(tmp_path),
    )
    assert not escaped_path.allowed
    assert "outside its assigned workspace" in escaped_path.reason


def test_action_grants_are_exact_and_single_use() -> None:
    registry = ActionGrantRegistry()
    spec = AgentSpec(
        "operator", "interact", "bounded",
        ("mcp__playwright__browser_click",),
        capabilities=(AgentCapability.BROWSER_READ, AgentCapability.BROWSER_INTERACTION),
    )
    arguments = {"ref": "button-1"}
    grant = registry.issue(
        root_run_id="root", child_run_id="child", tool="mcp__playwright__browser_click",
        arguments=arguments, request_id="request", explicit_user_confirmation=True,
    )
    mismatch = authorize_tool_call(
        spec, "mcp__playwright__browser_click",
        {"ref": "button-2", "action_grant_id": grant.grant_id},
        grant_registry=registry, root_run_id="root", child_run_id="child", request_id="request",
    )
    assert not mismatch.allowed

    granted_args = {**arguments, "action_grant_id": grant.grant_id}
    assert authorize_tool_call(
        spec, "mcp__playwright__browser_click", granted_args,
        grant_registry=registry, root_run_id="root", child_run_id="child", request_id="request",
    ).allowed
    replay = authorize_tool_call(
        spec, "mcp__playwright__browser_click", granted_args,
        grant_registry=registry, root_run_id="root", child_run_id="child", request_id="request",
    )
    assert not replay.allowed
    assert "already" in replay.reason


def test_child_shell_blocks_opaque_interpreters_and_git_push_even_with_grant() -> None:
    registry = ActionGrantRegistry()
    spec = AgentSpec(
        "builder", "build", "bounded", ("run_command",), can_mutate=True,
        capabilities=(
            AgentCapability.SHELL_EXECUTION,
            AgentCapability.FILESYSTEM_WRITE,
            AgentCapability.EXTERNAL_MUTATION,
        ),
    )
    opaque = authorize_tool_call(
        spec, "run_command", {"command": "python -c \"import requests; requests.post('https://x')\""},
        grant_registry=registry, root_run_id="root", child_run_id="child", request_id="request",
    )
    assert not opaque.allowed
    assert "nested interpreters" in opaque.reason

    arguments = {"command": "git push origin topic"}
    grant = registry.issue(
        root_run_id="root", child_run_id="child", tool="run_command",
        arguments=arguments, request_id="request", explicit_user_confirmation=True,
    )
    denied_push = authorize_tool_call(
        spec, "run_command", {**arguments, "action_grant_id": grant.grant_id},
        grant_registry=registry, root_run_id="root", child_run_id="child", request_id="request",
    )
    assert not denied_push.allowed
    assert "cannot push Git" in denied_push.reason


@pytest.mark.parametrize(
    ("tool", "capability"),
    (("run_command", AgentCapability.SHELL_EXECUTION), ("run_python", AgentCapability.CODE_EXECUTION)),
)
def test_execution_capability_without_filesystem_write_cannot_mutate_indirectly(
    tool: str, capability: AgentCapability
) -> None:
    spec = AgentSpec(
        "read_only_executor",
        "inspect in a sandbox",
        "never mutate",
        (tool,),
        capabilities=(AgentCapability.FILESYSTEM_READ, capability),
    )
    schema = {"type": "function", "function": {"name": tool, "parameters": {"type": "object"}}}
    assert filter_tool_schemas([schema], spec) == []
    decision = authorize_tool_call(
        spec,
        tool,
        {"command": "echo unsafe", "code": "open('unsafe', 'w').write('x')", "cwd": "."},
        workspace_root=".",
    )
    assert not decision.allowed
    assert "filesystem_write" in decision.reason


@pytest.mark.asyncio
async def test_resource_coordinator_overlaps_reads_and_serializes_conflicting_writes(tmp_path: Path) -> None:
    coordinator = ResourceCoordinator()
    active = 0
    peak = 0

    async def use(name: str, path: Path) -> None:
        nonlocal active, peak
        async with coordinator.acquire_call(name, {"path": str(path)}):
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.03)
            active -= 1

    await asyncio.gather(
        use("read_file", tmp_path / "a"), use("read_file", tmp_path / "a")
    )
    assert peak == 2
    peak = 0
    await asyncio.gather(
        use("write_file", tmp_path / "a"), use("read_file", tmp_path / "a")
    )
    assert peak == 1

    async def use_database(name: str) -> None:
        nonlocal active, peak
        async with coordinator.acquire_call(name, {}):
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.03)
            active -= 1

    peak = 0
    await asyncio.gather(use_database("search_memory"), use_database("store_memory"))
    assert peak == 1


@pytest.mark.asyncio
async def test_filesystem_writer_queue_prevents_late_read_from_overtaking(
    tmp_path: Path,
) -> None:
    coordinator = ResourceCoordinator()
    path = tmp_path / "ordered.txt"
    first_entered = asyncio.Event()
    release_first = asyncio.Event()
    writer_entered = asyncio.Event()
    release_writer = asyncio.Event()
    late_reader_entered = asyncio.Event()
    order: list[str] = []

    async def first_reader() -> None:
        async with coordinator.acquire_call("read_file", {"path": str(path)}):
            order.append("first_read")
            first_entered.set()
            await release_first.wait()

    async def writer() -> None:
        async with coordinator.acquire_call("write_file", {"path": str(path)}):
            order.append("write")
            writer_entered.set()
            await release_writer.wait()

    async def late_reader() -> None:
        async with coordinator.acquire_call("read_file", {"path": str(path)}):
            order.append("late_read")
            late_reader_entered.set()

    first_task = asyncio.create_task(first_reader())
    await first_entered.wait()
    writer_task = asyncio.create_task(writer())
    for _ in range(20):
        if coordinator.state()["filesystem_waiters"]:
            break
        await asyncio.sleep(0)
    late_task = asyncio.create_task(late_reader())
    await asyncio.sleep(0)
    release_first.set()
    await writer_entered.wait()
    assert not late_reader_entered.is_set()
    release_writer.set()
    await asyncio.gather(first_task, writer_task, late_task)
    assert order == ["first_read", "write", "late_read"]


def test_builder_worktree_manager_has_safe_non_git_fallback(tmp_path: Path) -> None:
    manager = BuilderWorktreeManager(tmp_path / "worktrees")
    repository = tmp_path / "plain"
    repository.mkdir()
    workspace = manager.prepare(repository, root_run_id="root", child_run_id="child")
    assert not workspace.isolated
    assert "serialized" in workspace.reason


def test_isolated_builder_worktree_captures_and_applies_reviewable_patch(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    for command in (
        ("git", "init", str(repository)),
        ("git", "-C", str(repository), "config", "user.email", "tests@example.test"),
        ("git", "-C", str(repository), "config", "user.name", "Ares Tests"),
    ):
        assert subprocess.run(command, capture_output=True, text=True, check=False).returncode == 0
    (repository / "tracked.txt").write_text("before\n", encoding="utf-8")
    assert subprocess.run(
        ("git", "-C", str(repository), "add", "tracked.txt"), capture_output=True, text=True, check=False
    ).returncode == 0
    assert subprocess.run(
        ("git", "-C", str(repository), "commit", "-m", "base"), capture_output=True, text=True, check=False
    ).returncode == 0

    manager = BuilderWorktreeManager(tmp_path / "worktrees")
    workspace = manager.prepare(repository, root_run_id="ma_test", child_run_id="agent_test")
    assert workspace.isolated
    isolated_root = Path(workspace.root)
    try:
        (isolated_root / "tracked.txt").write_text("after\n", encoding="utf-8")
        (isolated_root / "created.txt").write_text("new file\n", encoding="utf-8")
        patch_path, message = manager.capture_patch(
            workspace, root_run_id="ma_test", child_run_id="agent_test"
        )
        assert patch_path is not None, message
        patch = Path(patch_path)
        assert patch.is_file()
        patch_text = patch.read_text(encoding="utf-8")
        assert "tracked.txt" in patch_text and "created.txt" in patch_text

        applied, detail = manager.apply_patch(repository, patch)
        assert applied, detail
        assert (repository / "tracked.txt").read_text(encoding="utf-8") == "after\n"
        assert (repository / "created.txt").read_text(encoding="utf-8") == "new file\n"
    finally:
        subprocess.run(
            ("git", "-C", str(repository), "worktree", "remove", "--force", str(isolated_root)),
            capture_output=True,
            text=True,
            check=False,
        )


def test_reviewer_approval_holds_isolated_patch_without_exact_root_grant(tmp_path: Path) -> None:
    patch = tmp_path / "builder.patch"
    patch.write_text("diff --git a/a b/a\n", encoding="utf-8")
    runtime = MultiAgentRuntime(_Root(tmp_path, persist_runs=False))
    tasks = (
        AgentTask("build", "builder", "Implement"),
        AgentTask("review_build", "reviewer", "Review", depends_on=("build",)),
    )
    results = (
        AgentResult("build", "builder", AgentRunStatus.SUCCEEDED, content="done", metadata={"patch_path": str(patch)}),
        AgentResult("review_build", "reviewer", AgentRunStatus.SUCCEEDED, content="APPROVE_PATCH"),
    )
    applications = runtime._apply_approved_builder_patches(
        tasks=tasks,
        results=results,
        builder_workspaces={"build": {"root": str(tmp_path), "isolated": True, "child_run_id": "child"}},
        manager=BuilderWorktreeManager(tmp_path / "worktrees"),
        review_role="reviewer",
        auto_apply=True,
    )
    assert applications["build"]["status"] == "held_for_root_approval"
    assert applications["build"]["patch_hash"]


def test_run_store_scopes_children_to_parent_session_and_supports_latest(tmp_path: Path) -> None:
    store = MultiAgentRunStore(tmp_path)
    store.upsert({
        "run_id": "root", "root_run_id": "root", "session_id": "session-a",
        "request_id": "request-a", "agent_role": "supervisor", "status": "succeeded",
    })
    store.upsert({
        "run_id": "child", "root_run_id": "root", "parent_run_id": "root",
        "session_id": "agent:root:task:child", "parent_session_id": "session-a",
        "request_id": "request-a", "agent_role": "analyst", "status": "succeeded",
    })
    assert store.get("root", session_id="session-b") is None
    assert store.get("child", session_id="session-a") is not None
    assert store.latest(session_id="session-a")["run_id"] == "root"  # type: ignore[index]
    store.close()


class _Root:
    def __init__(self, data_dir: Path, **config: object) -> None:
        self.config = AppConfig(
            data_dir=str(data_dir), multi_agent=MultiAgentConfig(**config)
        )


def test_apply_config_reconciles_persistence_and_data_dir(tmp_path: Path) -> None:
    root = _Root(tmp_path / "one", persist_runs=False)
    runtime = MultiAgentRuntime(root)
    assert runtime.store is None

    root.config.multi_agent.persist_runs = True
    runtime.apply_config()
    assert runtime.store is not None
    first_store = runtime.store

    root.config.data_dir = str(tmp_path / "two")
    runtime.apply_config()
    assert runtime.store is not None
    assert runtime.store.db_path.parent == (tmp_path / "two")
    with pytest.raises(Exception):
        first_store.conn.execute("SELECT 1")

    root.config.multi_agent.persist_runs = False
    runtime.apply_config()
    assert runtime.store is None


@pytest.mark.asyncio
async def test_disabled_reviewer_rejects_mutation_with_clear_error(tmp_path: Path) -> None:
    runtime = MultiAgentRuntime(_Root(
        tmp_path,
        role_overrides={"reviewer": {"enabled": False}},
        require_review_for_mutations=True,
    ))
    with pytest.raises(RuntimeError, match="review role"):
        await runtime.delegate([AgentTask("build", "builder", "Implement")])
    await runtime.close()


@pytest.mark.asyncio
async def test_runtime_manifest_uses_unique_child_sessions_and_scopes_truth(tmp_path: Path) -> None:
    runtime = MultiAgentRuntime(_Root(tmp_path))

    async def executor(spec, task, context):
        return AgentOutput(
            "done", "done",
            metadata={
                "run_id": context.run_metadata["child_run_ids"][task.task_id],
                "root_run_id": context.run_metadata["root_run_id"],
                "parent_run_id": context.run_metadata["root_run_id"],
                "session_id": context.run_metadata["child_session_ids"][task.task_id],
                "tools": ["read_file"],
            },
        )

    runtime.adapter = executor  # type: ignore[assignment]
    result = await runtime.delegate(
        [AgentTask("inspect", "analyst", "Inspect")],
        session_id="session-a", request_id="request-a",
    )
    assert result.manifest is not None
    child = result.manifest.child_runs[0]
    assert child.session_id.startswith("agent:")
    assert child.session_id != result.manifest.session_id
    assert child.parent_session_id == "session-a"
    assert runtime.get_run(result.root_run_id, session_id="session-b") is None
    assert runtime.get_latest_run(session_id="session-a")["run_id"] == result.root_run_id  # type: ignore[index]
    await runtime.close()


@pytest.mark.asyncio
async def test_cancellation_emits_events_only_for_unfinished_children(tmp_path: Path) -> None:
    runtime = MultiAgentRuntime(_Root(tmp_path))
    events: list[dict] = []
    runtime.subscribe(events.append)
    second_started = asyncio.Event()

    async def executor(spec, task, context):
        if task.task_id == "first":
            return "complete"
        second_started.set()
        await asyncio.sleep(30)
        return "late"

    runtime.adapter = executor  # type: ignore[assignment]
    pending = asyncio.create_task(runtime.delegate([
        AgentTask("first", "analyst", "First"),
        AgentTask("second", "analyst", "Second", depends_on=("first",)),
    ], session_id="session-a"))
    await asyncio.wait_for(second_started.wait(), timeout=1)
    root_run_id = next(
        key for key, value in runtime._volatile.items() if key == value.get("root_run_id")
    )
    assert await runtime.cancel(root_run_id, session_id="session-a")
    with pytest.raises(asyncio.CancelledError):
        await pending
    cancelled_tasks = {
        event.get("task_id") for event in events if event.get("event_type") == "agent_cancelled"
    }
    assert cancelled_tasks == {"second"}
    first = next(
        child for child in runtime.get_run(root_run_id, session_id="session-a")["children"]  # type: ignore[index]
        if child["task_id"] == "first"
    )
    assert first["status"] == "succeeded"
    await runtime.close()


@pytest.mark.asyncio
async def test_partial_dependency_synthesis_runs_after_specialist_failure() -> None:
    seen_status: AgentRunStatus | None = None

    async def executor(spec, task, context: AgentExecutionContext):
        nonlocal seen_status
        if task.task_id == "source":
            raise RuntimeError("provider failed")
        seen_status = context.dependency_results["source"].status
        return "partial synthesis"

    specs = (
        AgentSpec("researcher", "research", "read"),
        AgentSpec("synthesizer", "synthesize", "read"),
    )
    from ares.multi_agent import AgentRegistry
    orchestrator = MultiAgentOrchestrator(AgentRegistry(specs), executor)
    result = await orchestrator.run([
        AgentTask("source", "researcher", "Research"),
        AgentTask(
            "summary", "synthesizer", "Synthesize", depends_on=("source",),
            allow_partial_dependencies=True,
            allowed_context=("task_dependencies",),
        ),
    ])
    assert result.results[0].status is AgentRunStatus.FAILED
    assert result.results[1].status is AgentRunStatus.SUCCEEDED
    assert seen_status is AgentRunStatus.FAILED


@pytest.mark.asyncio
async def test_retry_uses_fallback_only_when_adapter_marks_failure_safe() -> None:
    attempts: list[tuple[int, str]] = []

    async def executor(spec, task, context: AgentExecutionContext):
        attempts.append((
            int(context.run_metadata["attempt"]),
            str(context.run_metadata["fallback_model"]),
        ))
        if len(attempts) == 1:
            raise RetryableAgentError("provider rate limit", retry_safe=True)
        return "recovered"

    from ares.multi_agent import AgentRegistry
    spec = AgentSpec(
        "researcher", "research", "read", retry_limit=1,
        retry_backoff_seconds=0, fallback_models=("fallback-model",),
    )
    result = await MultiAgentOrchestrator(
        AgentRegistry((spec,)), executor
    ).run([AgentTask("source", "researcher", "Research")])
    assert result.results[0].status is AgentRunStatus.SUCCEEDED
    assert attempts == [(0, ""), (1, "fallback-model")]


@pytest.mark.asyncio
async def test_total_run_deadline_times_out_without_retrying_consequential_work() -> None:
    calls = 0

    async def executor(spec, task, context):
        nonlocal calls
        calls += 1
        await asyncio.sleep(1)
        return "late"

    from ares.multi_agent import AgentRegistry
    spec = AgentSpec(
        "builder", "build", "bounded", timeout_seconds=10,
        retry_limit=2, can_mutate=True,
    )
    result = await MultiAgentOrchestrator(
        AgentRegistry((spec,)), executor
    ).run(
        [AgentTask("build", "builder", "Build")],
        max_duration_seconds=0.03,
    )
    assert result.results[0].status is AgentRunStatus.TIMED_OUT
    assert calls == 1


def test_research_claims_require_evidence_and_confidence_cannot_inflate() -> None:
    unsupported = ResearchClaim(
        "Framework handles 10,000 requests/s", (), (), 0.9
    )
    assert "exact numeric claim lacks source evidence" in validate_research_claim(unsupported)

    source = ResearchClaim(
        "Measured throughput was 1,000 requests/s",
        ("https://example.test/benchmark",),
        ("Table 2 reports 1,000 requests/s.",),
        0.6,
        benchmark_conditions=("single worker, test hardware X",),
    )
    assert synthesis_confidence((source,), 0.95) == 0.6
    parsed = parse_research_claims(
        '{"claims":[{"claim":"Measured throughput was 1,000 requests/s",'
        '"source_urls":["https://example.test/benchmark"],'
        '"evidence":["Table 2 reports 1,000 requests/s."],"confidence":0.6,'
        '"benchmark_conditions":["single worker"]}]}'
    )
    assert parsed.valid


@pytest.mark.asyncio
async def test_adapter_passes_bounded_context_and_unique_child_session(monkeypatch, tmp_path: Path) -> None:
    created: dict[str, object] = {}

    class FakeLLM:
        def __init__(self, **kwargs):
            self.model = kwargs["model"]

    class FakeAgent:
        def __init__(self, **kwargs):
            created.update(kwargs)
            self.last_iteration_count = 1

        @contextmanager
        def session_scope(self, session_id):
            created["scoped_session"] = session_id
            yield

        async def run_stream(self, prompt, history):
            assert history == []
            yield "bounded result"

        async def close(self):
            return None

    monkeypatch.setattr("ares.multi_agent_adapter.LLMClient", FakeLLM)
    monkeypatch.setattr("ares.multi_agent_adapter.Agent", FakeAgent)
    root = SimpleNamespace(
        config=AppConfig(data_dir=str(tmp_path)), memory_store=object(), conversation_store=object(),
        mcp_manager=None, _session_store=object(), tool_executor=object(), browser_controller=object(),
        _playwright_tool_lock=asyncio.Lock(), skill_manager=object(),
    )
    adapter = AresAgentAdapter(root)
    output = await adapter(
        AgentSpec("analyst", "Analyze", "Read"),
        AgentTask("inspect", "analyst", "Inspect", allowed_context=("project_files",)),
        AgentExecutionContext(
            "bounded",
            run_metadata={
                "root_run_id": "root", "child_run_ids": {"inspect": "child"},
                "child_session_ids": {"inspect": "agent:root:inspect:child"},
                "parent_session_id": "parent",
            },
            context_mode=ContextMode.BOUNDED_SPECIALIST,
            allowed_context=("project_files",),
        ),
    )
    assert created["session_id"] == "agent:root:inspect:child"
    assert created["scoped_session"] == "agent:root:inspect:child"
    assert created["context_mode"] is ContextMode.BOUNDED_SPECIALIST
    assert output.metadata["parent_session_id"] == "parent"
