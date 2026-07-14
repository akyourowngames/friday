from __future__ import annotations

import asyncio
import time

import pytest

from ares.multi_agent import (
    AgentExecutionContext,
    AgentOutput,
    AgentRegistry,
    AgentRunStatus,
    AgentSpec,
    AgentTask,
    MultiAgentOrchestrator,
    default_agent_specs,
    validate_task_graph,
)


def _registry(*names: str, timeout: float = 1.0) -> AgentRegistry:
    return AgentRegistry(
        AgentSpec(
            name=name,
            description=f"{name} specialist",
            instructions="Do the assigned task.",
            timeout_seconds=timeout,
        )
        for name in names
    )


def test_default_team_has_separated_mutation_boundary() -> None:
    specs = {spec.name: spec for spec in default_agent_specs()}

    assert specs["builder"].can_mutate is True
    assert "write_file" in specs["builder"].allowed_tools
    assert specs["reviewer"].can_mutate is False
    assert "write_file" not in specs["reviewer"].allowed_tools
    assert specs["researcher"].permits_tool("web_search")


def test_registry_rejects_duplicate_agent() -> None:
    registry = _registry("researcher")
    with pytest.raises(ValueError, match="already registered"):
        registry.register(registry.get("researcher"))


def test_graph_validation_rejects_missing_dependency() -> None:
    with pytest.raises(ValueError, match="unknown task dependencies"):
        validate_task_graph(
            [AgentTask("write", "builder", "Implement", depends_on=("research",))]
        )


def test_graph_validation_rejects_cycle() -> None:
    tasks = [
        AgentTask("a", "planner", "A", depends_on=("b",)),
        AgentTask("b", "planner", "B", depends_on=("a",)),
    ]
    with pytest.raises(ValueError, match="cycle"):
        validate_task_graph(tasks)


@pytest.mark.asyncio
async def test_independent_tasks_run_in_parallel() -> None:
    active = 0
    peak = 0

    async def executor(spec, task, context):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.08)
        active -= 1
        return AgentOutput(content=task.task_id)

    orchestrator = MultiAgentOrchestrator(
        _registry("researcher"), executor, max_parallel=3
    )
    started = time.perf_counter()
    result = await orchestrator.run(
        [
            AgentTask("one", "researcher", "First"),
            AgentTask("two", "researcher", "Second"),
            AgentTask("three", "researcher", "Third"),
        ]
    )
    elapsed = time.perf_counter() - started

    assert peak == 3
    assert elapsed < 0.18
    assert [item.content for item in result.results] == ["one", "two", "three"]


@pytest.mark.asyncio
async def test_parallelism_is_bounded() -> None:
    active = 0
    peak = 0

    async def executor(spec, task, context):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.03)
        active -= 1
        return "done"

    orchestrator = MultiAgentOrchestrator(_registry("worker"), executor, max_parallel=2)
    await orchestrator.run(
        [AgentTask(str(index), "worker", f"Task {index}") for index in range(6)]
    )

    assert peak == 2


@pytest.mark.asyncio
async def test_dependencies_execute_in_waves_and_receive_results() -> None:
    order: list[str] = []

    async def executor(spec, task, context: AgentExecutionContext):
        order.append(task.task_id)
        if task.task_id == "synthesize":
            assert set(context.dependency_results) == {"research", "analysis"}
            assert context.dependency_results["research"].content == "research-result"
        return AgentOutput(content=f"{task.task_id}-result")

    orchestrator = MultiAgentOrchestrator(
        _registry("researcher", "analyst", "synthesizer"),
        executor,
        max_parallel=2,
    )
    result = await orchestrator.run(
        [
            AgentTask("research", "researcher", "Research"),
            AgentTask("analysis", "analyst", "Analyze"),
            AgentTask(
                "synthesize",
                "synthesizer",
                "Combine",
                depends_on=("research", "analysis"),
            ),
        ]
    )

    assert set(order[:2]) == {"research", "analysis"}
    assert order[-1] == "synthesize"
    assert result.results[-1].status is AgentRunStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_failure_is_isolated_and_dependant_is_blocked() -> None:
    async def executor(spec, task, context):
        if task.task_id == "bad":
            raise RuntimeError("boom")
        return "ok"

    orchestrator = MultiAgentOrchestrator(_registry("worker"), executor, max_parallel=3)
    result = await orchestrator.run(
        [
            AgentTask("good", "worker", "Good"),
            AgentTask("bad", "worker", "Bad"),
            AgentTask("after_bad", "worker", "After", depends_on=("bad",)),
        ]
    )
    by_id = result.by_id()

    assert by_id["good"].status is AgentRunStatus.SUCCEEDED
    assert by_id["bad"].status is AgentRunStatus.FAILED
    assert by_id["after_bad"].status is AgentRunStatus.BLOCKED


@pytest.mark.asyncio
async def test_timeout_is_normalized() -> None:
    async def executor(spec, task, context):
        await asyncio.sleep(0.08)
        return "late"

    orchestrator = MultiAgentOrchestrator(
        _registry("slow", timeout=0.02), executor
    )
    result = await orchestrator.run([AgentTask("slow-task", "slow", "Wait")])

    assert result.results[0].status is AgentRunStatus.TIMED_OUT
    assert "timeout" in (result.results[0].error or "")


@pytest.mark.asyncio
async def test_fail_fast_cancels_remaining_required_work() -> None:
    async def executor(spec, task, context):
        if task.task_id == "first":
            raise ValueError("stop")
        return "not reached"

    orchestrator = MultiAgentOrchestrator(
        _registry("worker"), executor, max_parallel=1, fail_fast=True
    )
    result = await orchestrator.run(
        [
            AgentTask("first", "worker", "Fail"),
            AgentTask("second", "worker", "Next", depends_on=("first",)),
        ]
    )

    assert result.results[0].status is AgentRunStatus.FAILED
    assert result.results[1].status in {AgentRunStatus.BLOCKED, AgentRunStatus.CANCELLED}


@pytest.mark.asyncio
async def test_progress_callback_supports_sync_callbacks() -> None:
    phases: list[str] = []

    def callback(event):
        phases.append(event.phase)

    async def executor(spec, task, context):
        return AgentOutput(content="done", summary="Finished")

    orchestrator = MultiAgentOrchestrator(_registry("worker"), executor)
    await orchestrator.run(
        [AgentTask("task", "worker", "Do it")], progress_callback=callback
    )

    assert phases == ["queued", "running", "succeeded"]
