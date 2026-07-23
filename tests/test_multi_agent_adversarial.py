from __future__ import annotations

import asyncio
import json
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from ares.agent import Agent
from ares.integrations.delegation_router import DelegationDecision, DelegationMode
from ares.models import AppConfig, MultiAgentConfig
from ares.multi_agent import (
    AgentExecutionContext,
    AgentOutput,
    AgentRegistry,
    AgentResult,
    AgentRunStatus,
    AgentSpec,
    AgentTask,
    ContextMode,
    MultiAgentOrchestrator,
    RetryableAgentError,
)
from ares.multi_agent.adapter import AresAgentAdapter
from ares.multi_agent.runtime import MultiAgentRuntime
from ares.multi_agent.store import MultiAgentRunStore
from ares.infra.server import AresServer
from ares.integrations.tool_registry import select_root_tools
from ares.integrations.turn_policy import build_turn_execution_context
from ares.workspace.app import create_workspace_app


class _Root:
    def __init__(self, data_dir: Path, **multi_agent: object) -> None:
        self.config = AppConfig(
            data_dir=str(data_dir),
            multi_agent=MultiAgentConfig(**multi_agent),
        )


@pytest.mark.asyncio
async def test_runtime_scope_blocks_cross_session_list_child_and_cancel_then_survives_reopen(
    tmp_path: Path,
) -> None:
    runtime = MultiAgentRuntime(_Root(tmp_path))
    started = asyncio.Event()

    async def slow_executor(spec, task, context):
        started.set()
        await asyncio.Event().wait()

    runtime.adapter = slow_executor  # type: ignore[assignment]
    pending = asyncio.create_task(
        runtime.delegate(
            [AgentTask("inspect", "analyst", "Inspect bounded state")],
            session_id="session-owner",
            request_id="request-owner",
        )
    )
    await asyncio.wait_for(started.wait(), timeout=1)
    root_run_id = next(
        run_id
        for run_id, record in runtime._volatile.items()
        if run_id == record.get("root_run_id")
    )
    owner_record = runtime.get_run(root_run_id, session_id="session-owner")
    assert owner_record is not None
    child_run_id = owner_record["children"][0]["run_id"]

    assert runtime.list_runs(session_id="session-attacker") == []
    assert runtime.get_run(root_run_id, session_id="session-attacker") is None
    assert runtime.get_run(child_run_id, session_id="session-attacker") is None
    assert await runtime.cancel(root_run_id, session_id="session-attacker") is False
    assert not pending.done()

    assert runtime.get_run(child_run_id, session_id="session-owner") is not None
    assert await runtime.cancel(child_run_id, session_id="session-owner") is True
    with pytest.raises(asyncio.CancelledError):
        await pending

    cancelled = runtime.get_run(root_run_id, session_id="session-owner")
    assert cancelled is not None
    assert cancelled["status"] == "cancelled"
    assert cancelled["manifest"]["status"] == "cancelled"
    assert cancelled["manifest"]["root_run_id"] == root_run_id
    assert cancelled["manifest"]["session_id"] == "session-owner"
    assert cancelled["manifest"]["request_id"] == "request-owner"
    assert len(cancelled["manifest"]["child_runs"]) == 1
    assert cancelled["manifest"]["child_runs"][0]["status"] == "cancelled"
    assert cancelled["manifest"]["completed_at"]

    await runtime.close()

    reopened = MultiAgentRunStore(tmp_path)
    try:
        restored = reopened.get(root_run_id, session_id="session-owner")
        assert restored is not None
        assert restored["manifest"]["status"] == "cancelled"
        assert reopened.get(root_run_id, session_id="session-attacker") is None
        assert reopened.get(child_run_id, session_id="session-owner") is not None
        assert reopened.get(child_run_id, session_id="session-attacker") is None
        assert reopened.list(session_id="session-attacker") == []
    finally:
        reopened.close()


@pytest.mark.asyncio
async def test_retry_safe_false_never_retries_or_selects_fallback() -> None:
    attempts: list[tuple[int, str]] = []

    async def executor(spec, task, context: AgentExecutionContext):
        attempts.append(
            (
                int(context.run_metadata["attempt"]),
                str(context.run_metadata["fallback_model"]),
            )
        )
        raise RetryableAgentError(
            "transport failed after consequential work",
            retry_safe=False,
            iterations=1,
        )

    spec = AgentSpec(
        "builder",
        "Build",
        "Mutate bounded files",
        retry_limit=3,
        retry_backoff_seconds=0,
        fallback_models=("fallback-a", "fallback-b", "fallback-c"),
        can_mutate=True,
    )
    result = await MultiAgentOrchestrator(
        AgentRegistry((spec,)), executor
    ).run([AgentTask("build", "builder", "Implement")])

    assert attempts == [(0, "")]
    assert result.results[0].status is AgentRunStatus.FAILED
    assert "RetryableAgentError" in (result.results[0].error or "")


@pytest.mark.asyncio
async def test_retry_iterations_are_aggregate_not_reset_per_attempt() -> None:
    attempts = 0

    async def executor(spec, task, context: AgentExecutionContext):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RetryableAgentError(
                "safe pre-tool provider failure",
                retry_safe=True,
                iterations=2,
            )
        assert context.run_metadata["remaining_iterations"] == 1
        return AgentOutput("late success", metadata={"iterations": 2})

    spec = AgentSpec(
        "analyst",
        "Analyze",
        "Read bounded inputs",
        max_iterations=3,
        retry_limit=1,
        retry_backoff_seconds=0,
    )
    result = await MultiAgentOrchestrator(
        AgentRegistry((spec,)), executor
    ).run([AgentTask("inspect", "analyst", "Inspect")])

    assert attempts == 2
    assert result.results[0].status is AgentRunStatus.FAILED
    assert "aggregate retry iteration budget" in (result.results[0].error or "")


class _FakeLLM:
    def __init__(self, **kwargs) -> None:
        self.model = kwargs["model"]


class _PayloadAgent:
    payload = ""

    def __init__(self, **kwargs) -> None:
        self.last_iteration_count = 1

    @contextmanager
    def session_scope(self, session_id):
        yield

    async def run_stream(self, prompt, history):
        assert history == []
        yield self.payload

    async def close(self) -> None:
        return None


def _adapter_root(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        config=AppConfig(data_dir=str(tmp_path)),
        memory_store=object(),
        conversation_store=object(),
        mcp_manager=None,
        _session_store=object(),
        tool_executor=object(),
        browser_controller=object(),
        _playwright_tool_lock=asyncio.Lock(),
        skill_manager=object(),
    )


@pytest.mark.asyncio
async def test_adapter_enforces_specialist_output_token_budget(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("ares.multi_agent.adapter.LLMClient", _FakeLLM)
    monkeypatch.setattr("ares.multi_agent.adapter.Agent", _PayloadAgent)
    _PayloadAgent.payload = "This output is longer than one estimated token."
    adapter = AresAgentAdapter(_adapter_root(tmp_path))

    with pytest.raises(ValueError, match="estimated-token budget"):
        await adapter(
            AgentSpec(
                "analyst",
                "Analyze",
                "Read bounded inputs",
                max_output_tokens=1,
            ),
            AgentTask("inspect", "analyst", "Inspect"),
            AgentExecutionContext(
                "bounded",
                run_metadata={
                    "root_run_id": "root",
                    "child_run_ids": {"inspect": "child"},
                    "child_session_ids": {
                        "inspect": "agent:root:inspect:child"
                    },
                    "parent_session_id": "session-owner",
                },
            ),
        )


def _source_claim(claim: str, url: str, confidence: float) -> dict[str, object]:
    return {
        "claim": claim,
        "source_urls": [url],
        "evidence": [f"Source states: {claim}"],
        "confidence": confidence,
        "caveats": [],
        "publication_dates": [],
        "benchmark_conditions": [],
    }


@pytest.mark.asyncio
async def test_synthesizer_cannot_inflate_confidence_or_hide_source_conflict(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("ares.multi_agent.adapter.LLMClient", _FakeLLM)
    monkeypatch.setattr("ares.multi_agent.adapter.Agent", _PayloadAgent)
    positive = _source_claim(
        "The feature is enabled", "https://source.example/enabled", 0.6
    )
    negative = _source_claim(
        "The feature is not enabled", "https://source.example/disabled", 0.7
    )
    _PayloadAgent.payload = json.dumps(
        {
            "summary": "The feature is enabled.",
            "claims": [
                _source_claim(
                    "The feature is enabled",
                    "https://source.example/enabled",
                    0.95,
                )
            ],
            "disagreements": [],
            "caveats": [],
        }
    )
    dependency = AgentResult(
        "research",
        "researcher",
        AgentRunStatus.SUCCEEDED,
        content="source evidence",
        metadata={
            "research_validation": {
                "valid": True,
                "issues": [],
                "claims": [positive, negative],
            }
        },
    )
    adapter = AresAgentAdapter(_adapter_root(tmp_path))

    with pytest.raises(ValueError) as caught:
        await adapter(
            AgentSpec("synthesizer", "Synthesize", "Preserve evidence"),
            AgentTask(
                "synthesis",
                "synthesizer",
                "Synthesize",
                depends_on=("research",),
                allowed_context=("task_dependencies",),
            ),
            AgentExecutionContext(
                "bounded",
                dependency_results={"research": dependency},
                run_metadata={
                    "root_run_id": "root",
                    "child_run_ids": {"synthesis": "child"},
                    "child_session_ids": {
                        "synthesis": "agent:root:synthesis:child"
                    },
                    "parent_session_id": "session-owner",
                },
                context_mode=ContextMode.BOUNDED_SPECIALIST,
                allowed_context=("task_dependencies",),
            ),
        )

    message = str(caught.value)
    assert "exceeds source ceiling 0.700" in message
    assert "hides conflicting source claims" in message


def test_artifact_endpoint_rejects_direct_paths_and_expired_tokens(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    artifact = tmp_path / "private.pdf"
    artifact.write_bytes(b"%PDF-1.6\nprivate\n")
    app = create_workspace_app(
        artifact_roots=[tmp_path],
        artifact_resolver=lambda token: artifact if token == "opaque" else None,
    )
    with TestClient(app) as client:
        direct = client.get(
            "/api/artifact",
            params={"path": str(artifact)},
        )
        path_as_token = client.get(
            "/api/artifact",
            params={"token": str(artifact)},
        )
        encoded_path_as_token = client.get(
            f"/api/artifact?token={quote(str(artifact), safe='')}"
        )
        authorized = client.get("/api/artifact", params={"token": "opaque"})

    assert direct.status_code == 422
    assert path_as_token.status_code == 404
    assert encoded_path_as_token.status_code == 404
    assert authorized.status_code == 200

    server = AresServer.__new__(AresServer)
    server._artifact_preview_tokens = {
        "expired": (str(artifact), 7, 99.0),
    }
    monkeypatch.setattr("ares.infra.server.time.monotonic", lambda: 100.0)
    assert server._resolve_artifact_preview_token("expired") is None
    assert "expired" not in server._artifact_preview_tokens


def test_final_answer_replaces_hostile_agent_claims_and_preserves_verified_sources() -> None:
    agent = Agent.__new__(Agent)
    agent._execution_records = {}
    context = build_turn_execution_context(
        "Use an agent to research this",
        request_id="request-verified",
        session_id="session-owner",
    )
    agent._set_execution_record(
        context,
        {
            "kind": "native",
            "payload": {
                "manifest": {
                    "root_run_id": "ma_verified",
                    "status": "succeeded",
                    "agent_count": 1,
                    "execution_waves": [["research"]],
                    "duration_seconds": 0.25,
                    "child_runs": [
                        {
                            "role": "researcher",
                            "tools": ["web_search"],
                            "metadata": {
                                "research_validation": {
                                    "claims": [
                                        {
                                            "source_urls": [
                                                "https://source.example/verified"
                                            ]
                                        }
                                    ]
                                }
                            },
                        }
                    ],
                }
            },
        },
    )
    decision = DelegationDecision(
        DelegationMode.EXPLICIT,
        True,
        "Explicit native delegation requested",
    )

    answer = agent._guard_final_answer(
        context,
        decision,
        "99 agents ran in 12 parallel waves.\nThe verified finding remains useful.",
    )

    assert "99 agents" not in answer
    assert "12 parallel waves" not in answer
    assert "Verified native run ma_verified: 1 agent (researcher)" in answer
    assert "https://source.example/verified" in answer
    assert "The verified finding remains useful." in answer


def test_referential_delegation_requires_and_resolves_a_concrete_prior_user_task() -> None:
    resolved, error = Agent._resolve_referential_delegation(
        "Use agents for that",
        [
            {"role": "user", "content": "Compare FastAPI and Flask security."},
            {"role": "assistant", "content": "I can do that."},
        ],
    )
    assert error is None
    assert "Resolved prior task: Compare FastAPI and Flask security." in resolved

    unresolved, error = Agent._resolve_referential_delegation(
        "Use agents for that",
        [{"role": "assistant", "content": "What should I delegate?"}],
    )
    assert unresolved == "Use agents for that"
    assert error is not None
    assert "no concrete prior user task" in error


def _updated_app_config(
    current: AppConfig,
    *,
    data_dir: Path | None = None,
    **multi_agent_changes: object,
) -> AppConfig:
    payload = current.model_dump(mode="python")
    if data_dir is not None:
        payload["data_dir"] = str(data_dir)
    payload["multi_agent"].update(multi_agent_changes)
    return AppConfig.model_validate(payload)


@pytest.mark.asyncio
async def test_rejected_active_topology_reload_is_atomic_for_agent_and_runtime(
    tmp_path: Path,
) -> None:
    agent = Agent.__new__(Agent)
    original = AppConfig(
        data_dir=str(tmp_path / "original"),
        multi_agent=MultiAgentConfig(persist_runs=True),
    )
    agent.config = original
    agent.delegation_depth = 0
    runtime = MultiAgentRuntime(agent)
    agent.multi_agent_runtime = runtime
    started = asyncio.Event()

    async def slow_executor(spec, task, context):
        started.set()
        await asyncio.Event().wait()

    runtime.adapter = slow_executor  # type: ignore[assignment]
    pending = asyncio.create_task(
        runtime.delegate(
            [AgentTask("inspect", "analyst", "Inspect")],
            session_id="session-owner",
        )
    )
    await asyncio.wait_for(started.wait(), timeout=1)
    root_run_id = next(
        run_id
        for run_id, record in runtime._volatile.items()
        if run_id == record.get("root_run_id")
    )
    original_runtime_config = runtime.config
    original_store = runtime.store
    original_registry = runtime.registry.snapshot()
    candidate = _updated_app_config(
        original,
        data_dir=tmp_path / "replacement",
        persist_runs=False,
    )

    with pytest.raises(RuntimeError, match="while runs are active"):
        agent.apply_config(candidate)

    assert agent.config is original
    assert runtime.config is original_runtime_config
    assert runtime.store is original_store
    assert runtime.store is not None
    assert runtime.store.db_path.parent == (tmp_path / "original")
    assert runtime.registry.snapshot() == original_registry
    assert runtime.list_runs(session_id="session-owner")[0]["run_id"] == root_run_id

    assert await runtime.cancel(root_run_id, session_id="session-owner") is True
    with pytest.raises(asyncio.CancelledError):
        await pending
    await runtime.close()


@pytest.mark.asyncio
async def test_role_model_and_limit_reload_affects_only_future_runs(
    tmp_path: Path,
) -> None:
    root = _Root(
        tmp_path,
        max_total_iterations=20,
        max_total_tokens=1000,
        default_timeout_seconds=40,
        max_timeout_seconds=60,
        role_overrides={
            "analyst": {
                "model": "old-analyst-model",
                "max_iterations": 9,
                "max_output_tokens": 800,
                "timeout_seconds": 30,
            }
        },
    )
    runtime = MultiAgentRuntime(root)
    old_started = asyncio.Event()
    release_old = asyncio.Event()
    observed: dict[str, dict[str, object]] = {}

    async def recording_executor(
        spec: AgentSpec, task: AgentTask, context: AgentExecutionContext
    ) -> AgentOutput:
        observed[task.prompt] = {
            "model": spec.model,
            "max_iterations": spec.max_iterations,
            "max_output_tokens": spec.max_output_tokens,
            "timeout_seconds": spec.timeout_seconds,
            "remaining_iterations": context.run_metadata["remaining_iterations"],
        }
        if task.prompt == "old-run":
            old_started.set()
            await release_old.wait()
        return AgentOutput("done", metadata={"iterations": 1})

    runtime.adapter = recording_executor  # type: ignore[assignment]
    old_pending = asyncio.create_task(
        runtime.delegate(
            [AgentTask("old", "analyst", "old-run")],
            session_id="session-owner",
        )
    )
    await asyncio.wait_for(old_started.wait(), timeout=1)

    candidate = _updated_app_config(
        root.config,
        max_total_iterations=4,
        max_total_tokens=256,
        default_timeout_seconds=10,
        role_overrides={
            "analyst": {
                "model": "new-analyst-model",
                "max_iterations": 2,
                "max_output_tokens": 128,
                "timeout_seconds": 5,
            }
        },
    )
    runtime.apply_config(candidate)
    root.config = candidate

    live_analyst = next(
        item for item in runtime.list_agents() if item["name"] == "analyst"
    )
    assert live_analyst["model"] == "new-analyst-model"
    assert live_analyst["max_iterations"] == 2
    assert live_analyst["max_output_tokens"] == 128
    assert live_analyst["timeout_seconds"] == 5

    release_old.set()
    old_result = await old_pending
    new_result = await runtime.delegate(
        [AgentTask("new", "analyst", "new-run")],
        session_id="session-owner",
    )

    assert observed["old-run"] == {
        "model": "old-analyst-model",
        "max_iterations": 9,
        "max_output_tokens": 800,
        "timeout_seconds": 30.0,
        "remaining_iterations": 9,
    }
    assert observed["new-run"] == {
        "model": "new-analyst-model",
        "max_iterations": 2,
        "max_output_tokens": 128,
        "timeout_seconds": 5.0,
        "remaining_iterations": 2,
    }
    assert old_result.manifest is not None
    assert new_result.manifest is not None
    assert old_result.manifest.metadata["max_total_tokens"] == 1000
    assert new_result.manifest.metadata["max_total_tokens"] == 256
    await runtime.close()


@pytest.mark.asyncio
async def test_live_reviewer_disable_rejects_future_mutation_delegation(
    tmp_path: Path,
) -> None:
    root = _Root(tmp_path)
    runtime = MultiAgentRuntime(root)
    candidate = _updated_app_config(
        root.config,
        role_overrides={"reviewer": {"enabled": False}},
    )
    runtime.apply_config(candidate)
    root.config = candidate

    assert "reviewer" not in {item["name"] for item in runtime.list_agents()}
    with pytest.raises(RuntimeError, match="review role.*disabled or unavailable"):
        await runtime.delegate([AgentTask("build", "builder", "Implement")])
    assert runtime.list_runs() == []
    await runtime.close()


@pytest.mark.parametrize("cancel_active", (False, True))
@pytest.mark.asyncio
async def test_disable_reload_uses_explicit_drain_or_cancel_semantics(
    tmp_path: Path, cancel_active: bool
) -> None:
    root = _Root(tmp_path)
    runtime = MultiAgentRuntime(root)
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_executor(spec, task, context):
        started.set()
        await release.wait()
        return "completed under launch snapshot"

    runtime.adapter = slow_executor  # type: ignore[assignment]
    pending = asyncio.create_task(
        runtime.delegate(
            [AgentTask("inspect", "analyst", "Inspect")],
            session_id="session-owner",
        )
    )
    await asyncio.wait_for(started.wait(), timeout=1)
    root_run_id = next(
        run_id
        for run_id, record in runtime._volatile.items()
        if run_id == record.get("root_run_id")
    )
    disabled = _updated_app_config(
        root.config,
        enabled=False,
        cancel_active_on_disable=cancel_active,
    )

    runtime.apply_config(disabled)
    root.config = disabled
    with pytest.raises(RuntimeError, match="disabled"):
        await runtime.delegate([AgentTask("future", "analyst", "Future")])

    if cancel_active:
        with pytest.raises(asyncio.CancelledError):
            await pending
        record = runtime.get_run(root_run_id, session_id="session-owner")
        assert record is not None
        assert record["status"] == "cancelled"
        assert record["manifest"]["status"] == "cancelled"
    else:
        await asyncio.sleep(0)
        assert not pending.done()
        release.set()
        result = await pending
        assert result.status == "succeeded"
        record = runtime.get_run(root_run_id, session_id="session-owner")
        assert record is not None
        assert record["status"] == "succeeded"

    await runtime.close()


def _tool_schema(name: str) -> dict[str, object]:
    return {
        "type": "function",
        "function": {"name": name, "parameters": {"type": "object"}},
    }


def _selected_tool_names(prompt: str, inventory: list[dict[str, object]]) -> set[str]:
    context = build_turn_execution_context(prompt, request_id="request-tools")
    return {
        str(schema["function"]["name"])  # type: ignore[index]
        for schema in select_root_tools(inventory, context)
    }


def test_dynamic_tool_categories_follow_current_turn_targets() -> None:
    inventory = [
        _tool_schema(name)
        for name in (
            "get_current_datetime",
            "create_goal",
            "list_goals",
            "create_watcher",
            "list_watchers",
            "create_task",
            "list_tasks",
            "run_task",
            "read_file",
            "write_file",
            "run_command",
            "update_config",
            "send_email",
            "mcp__slack__send_message",
            "mcp__notion__update_page",
            "unknown_plugin_action",
        )
    ]

    assert _selected_tool_names("Create a goal named ship safely", inventory) == {
        "get_current_datetime",
        "create_goal",
        "list_goals",
    }
    assert _selected_tool_names("Create a watcher to monitor releases", inventory) == {
        "get_current_datetime",
        "create_watcher",
        "list_watchers",
    }
    assert _selected_tool_names("Update the Ares config settings", inventory) == {
        "get_current_datetime",
        "update_config",
    }
    assert _selected_tool_names("Edit the file app.py and run tests", inventory) == {
        "get_current_datetime",
        "read_file",
        "write_file",
        "run_command",
    }
    assert _selected_tool_names(
        "Continue task build-deadbeef", inventory
    ) == {
        "list_tasks",
        "run_task",
    }

    external = _selected_tool_names(
        "Send a Slack message using the Slack integration", inventory
    )
    assert "mcp__slack__send_message" in external
    assert external.isdisjoint(
        {"create_goal", "create_watcher", "create_task", "write_file", "run_command"}
    )
    assert "unknown_plugin_action" not in external


def _durable_task(
    task_id: str,
    agent: str,
    prompt: str,
    *,
    depends_on: tuple[str, ...] = (),
    allowed_context: tuple[str, ...] = (),
) -> dict[str, object]:
    return {
        "task_id": task_id,
        "agent": agent,
        "prompt": prompt,
        "depends_on": list(depends_on),
        "context": {"bounded": task_id},
        "timeout_seconds": 30.0,
        "required": True,
        "result_format": "text",
        "allowed_context": list(allowed_context),
        "allow_partial_dependencies": False,
    }


def _seed_orphaned_durable_run(
    data_dir: Path,
    *,
    root_run_id: str,
    tasks: list[dict[str, object]],
    statuses: dict[str, str],
    session_id: str = "session-owner",
) -> None:
    store = MultiAgentRunStore(data_dir)
    try:
        store.upsert(
            {
                "run_id": root_run_id,
                "root_run_id": root_run_id,
                "session_id": session_id,
                "request_id": f"request-{root_run_id}",
                "agent_role": "supervisor",
                "prompt_summary": "durable adversarial run",
                "status": "running",
                "created_at": "2026-07-15T00:00:00+00:00",
                "metadata": {
                    "launch_plan": {
                        "version": 1,
                        "tasks": tasks,
                        "shared_context": "persisted bounded context",
                        "depth": 1,
                    }
                },
            }
        )
        for index, task in enumerate(tasks, 1):
            task_id = str(task["task_id"])
            status = statuses[task_id]
            record: dict[str, object] = {
                "run_id": f"child-{root_run_id}-{task_id}",
                "root_run_id": root_run_id,
                "parent_run_id": root_run_id,
                "session_id": f"agent:{root_run_id}:{task_id}",
                "parent_session_id": session_id,
                "request_id": f"request-{root_run_id}",
                "task_id": task_id,
                "agent_role": str(task["agent"]),
                "prompt_summary": str(task["prompt"]),
                "status": status,
                "dependencies": list(task.get("depends_on") or []),
                "created_at": f"2026-07-15T00:00:0{index}+00:00",
            }
            if status == "succeeded":
                record.update(
                    {
                        "started_at": "2026-07-15T00:00:01+00:00",
                        "completed_at": "2026-07-15T00:00:02+00:00",
                        "result_content": f"persisted result for {task_id}",
                        "result_summary": f"persisted {task_id}",
                        "iterations": 2,
                        "metadata": {"tools": ["read_file"]},
                    }
                )
            store.upsert(record)
    finally:
        store.close()


@pytest.mark.asyncio
async def test_generated_launch_plan_survives_store_close_and_reopen(
    tmp_path: Path,
) -> None:
    runtime = MultiAgentRuntime(_Root(tmp_path))

    async def executor(spec, task, context):
        return AgentOutput(
            f"result for {task.task_id}",
            metadata={"iterations": 1},
        )

    runtime.adapter = executor  # type: ignore[assignment]
    result = await runtime.delegate(
        [
            AgentTask(
                "source",
                "analyst",
                "Inspect source",
                context={"path": "README.md"},
                timeout_seconds=12,
            ),
            AgentTask(
                "summary",
                "synthesizer",
                "Summarize source",
                depends_on=("source",),
                context={"format": "brief"},
                result_format="markdown",
                allowed_context=("task_dependencies",),
            ),
        ],
        shared_context="bounded launch context",
        session_id="session-owner",
        request_id="request-launch-plan",
    )
    root_run_id = result.root_run_id
    await runtime.close()

    first_reopen = MultiAgentRunStore(tmp_path)
    try:
        restored = first_reopen.get(root_run_id, session_id="session-owner")
        assert restored is not None
        plan = restored["metadata"]["launch_plan"]
        assert plan["version"] == 1
        assert plan["shared_context"] == "bounded launch context"
        assert plan["depth"] == 1
        assert [task["task_id"] for task in plan["tasks"]] == [
            "source",
            "summary",
        ]
        assert plan["tasks"][0]["context"] == {"path": "README.md"}
        assert plan["tasks"][0]["timeout_seconds"] == 12
        assert plan["tasks"][1]["depends_on"] == ["source"]
        assert plan["tasks"][1]["allowed_context"] == ["task_dependencies"]
        assert plan["tasks"][1]["result_format"] == "markdown"
    finally:
        first_reopen.close()

    second_reopen = MultiAgentRunStore(tmp_path)
    try:
        assert second_reopen.get(root_run_id)["metadata"]["launch_plan"] == plan  # type: ignore[index]
    finally:
        second_reopen.close()


def test_runtime_startup_marks_orphaned_records_interrupted_and_checkpoints_them(
    tmp_path: Path,
) -> None:
    tasks = [
        _durable_task("source", "analyst", "Inspect"),
        _durable_task(
            "followup",
            "analyst",
            "Follow up",
            depends_on=("source",),
            allowed_context=("task_dependencies",),
        ),
        _durable_task(
            "summary",
            "synthesizer",
            "Summarize",
            depends_on=("followup",),
            allowed_context=("task_dependencies",),
        ),
    ]
    _seed_orphaned_durable_run(
        tmp_path,
        root_run_id="ma-orphan",
        tasks=tasks,
        statuses={"source": "succeeded", "followup": "running", "summary": "queued"},
    )

    runtime = MultiAgentRuntime(_Root(tmp_path))
    record = runtime.get_run("ma-orphan", session_id="session-owner")
    assert record is not None
    assert record["status"] == "interrupted"
    statuses = {child["task_id"]: child["status"] for child in record["children"]}
    assert statuses == {
        "source": "succeeded",
        "followup": "interrupted",
        "summary": "interrupted",
    }
    assert record["checkpoint"]["completed"] == ["source"]
    assert record["checkpoint"]["remaining"] == ["followup", "summary"]
    assert record["checkpoint"]["terminal"] == statuses
    assert record["checkpoint"]["resume_supported"] is True
    assert record["checkpoint"]["resume_available_now"] is True
    asyncio.run(runtime.close())


@pytest.mark.asyncio
async def test_resume_reuses_success_and_reruns_only_unfinished_read_only_chain(
    tmp_path: Path,
) -> None:
    tasks = [
        _durable_task("source", "analyst", "Inspect"),
        _durable_task(
            "followup",
            "analyst",
            "Follow up",
            depends_on=("source",),
            allowed_context=("task_dependencies",),
        ),
        _durable_task(
            "summary",
            "synthesizer",
            "Summarize",
            depends_on=("followup",),
            allowed_context=("task_dependencies",),
        ),
    ]
    _seed_orphaned_durable_run(
        tmp_path,
        root_run_id="ma-resume-source",
        tasks=tasks,
        statuses={"source": "succeeded", "followup": "running", "summary": "queued"},
    )
    runtime = MultiAgentRuntime(_Root(tmp_path))
    calls: list[str] = []

    async def executor(
        spec: AgentSpec, task: AgentTask, context: AgentExecutionContext
    ) -> AgentOutput:
        calls.append(task.task_id)
        if task.task_id == "followup":
            assert context.dependency_results["source"].content == (
                "persisted result for source"
            )
        if task.task_id == "summary":
            assert context.dependency_results["followup"].content == (
                "new result for followup"
            )
        return AgentOutput(
            f"new result for {task.task_id}",
            summary=f"new {task.task_id}",
            metadata={"iterations": 1},
        )

    runtime.adapter = executor  # type: ignore[assignment]
    resumed = await runtime.resume(
        "ma-resume-source",
        session_id="session-owner",
        request_id="request-resumed",
    )

    assert calls == ["followup", "summary"]
    assert [item.task_id for item in resumed.results] == [
        "source",
        "followup",
        "summary",
    ]
    reused = resumed.results[0]
    assert reused.content == "persisted result for source"
    assert reused.iterations == 2
    assert reused.metadata["checkpoint_reused"] is True
    assert resumed.execution_waves == (("followup",), ("summary",))
    assert resumed.manifest is not None
    assert resumed.manifest.metadata["resumed_from"] == "ma-resume-source"

    new_record = runtime.get_run(resumed.root_run_id, session_id="session-owner")
    assert new_record is not None
    assert new_record["metadata"]["resumed_from"] == "ma-resume-source"
    assert new_record["metadata"]["checkpoint_reused_tasks"] == ["source"]
    assert new_record["checkpoint"]["completed"] == [
        "source",
        "followup",
        "summary",
    ]
    assert new_record["checkpoint"]["remaining"] == []
    await runtime.close()


@pytest.mark.asyncio
async def test_resume_is_denied_for_wrong_session_without_creating_a_run(
    tmp_path: Path,
) -> None:
    tasks = [_durable_task("inspect", "analyst", "Inspect")]
    _seed_orphaned_durable_run(
        tmp_path,
        root_run_id="ma-private-resume",
        tasks=tasks,
        statuses={"inspect": "running"},
    )
    runtime = MultiAgentRuntime(_Root(tmp_path))

    with pytest.raises(PermissionError, match="not found in this session"):
        await runtime.resume(
            "ma-private-resume",
            session_id="session-attacker",
        )

    assert runtime.list_runs(session_id="session-attacker") == []
    assert [
        item["run_id"] for item in runtime.list_runs(session_id="session-owner")
    ] == ["ma-private-resume"]
    await runtime.close()


@pytest.mark.asyncio
async def test_resume_is_denied_while_original_run_is_active(
    tmp_path: Path,
) -> None:
    runtime = MultiAgentRuntime(_Root(tmp_path))
    started = asyncio.Event()

    async def slow_executor(spec, task, context):
        started.set()
        await asyncio.Event().wait()

    runtime.adapter = slow_executor  # type: ignore[assignment]
    pending = asyncio.create_task(
        runtime.delegate(
            [AgentTask("inspect", "analyst", "Inspect")],
            session_id="session-owner",
        )
    )
    await asyncio.wait_for(started.wait(), timeout=1)
    root_run_id = next(
        run_id
        for run_id, record in runtime._volatile.items()
        if run_id == record.get("root_run_id")
    )

    with pytest.raises(RuntimeError, match="still active"):
        await runtime.resume(root_run_id, session_id="session-owner")
    assert len(runtime.list_runs(session_id="session-owner")) == 1

    assert await runtime.cancel(root_run_id, session_id="session-owner") is True
    with pytest.raises(asyncio.CancelledError):
        await pending
    await runtime.close()


@pytest.mark.asyncio
async def test_resume_refuses_unfinished_mutation_capable_specialist(
    tmp_path: Path,
) -> None:
    tasks = [_durable_task("build", "builder", "Implement bounded change")]
    _seed_orphaned_durable_run(
        tmp_path,
        root_run_id="ma-unsafe-resume",
        tasks=tasks,
        statuses={"build": "running"},
    )
    runtime = MultiAgentRuntime(_Root(tmp_path))

    with pytest.raises(
        PermissionError,
        match="unsafe automatic resume refused.*mutation-capable.*builder",
    ):
        await runtime.resume(
            "ma-unsafe-resume",
            session_id="session-owner",
        )

    assert [
        item["run_id"] for item in runtime.list_runs(session_id="session-owner")
    ] == ["ma-unsafe-resume"]
    await runtime.close()
