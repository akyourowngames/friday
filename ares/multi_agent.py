"""Native, framework-neutral multi-agent orchestration for Ares."""

from __future__ import annotations

import asyncio
import inspect
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any, Protocol, TypeAlias


class AgentRunStatus(str, Enum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class AgentSpec:
    """Reusable specialist definition with a strict tool allowlist."""

    name: str
    description: str
    instructions: str
    allowed_tools: tuple[str, ...] = ()
    model: str | None = None
    max_iterations: int = 8
    timeout_seconds: float = 120.0
    can_delegate: bool = False
    can_mutate: bool = False

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("agent name cannot be empty")
        if self.max_iterations < 1:
            raise ValueError("max_iterations must be at least 1")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        object.__setattr__(self, "name", self.name.strip())
        object.__setattr__(self, "allowed_tools", tuple(dict.fromkeys(self.allowed_tools)))

    def permits_tool(self, name: str) -> bool:
        return name in self.allowed_tools


@dataclass(frozen=True, slots=True)
class AgentTask:
    task_id: str
    agent: str
    prompt: str
    depends_on: tuple[str, ...] = ()
    context: Mapping[str, Any] = field(default_factory=dict)
    timeout_seconds: float | None = None
    required: bool = True

    def __post_init__(self) -> None:
        if not self.task_id.strip() or not self.agent.strip() or not self.prompt.strip():
            raise ValueError("task_id, agent, and prompt are required")
        if self.task_id in self.depends_on:
            raise ValueError("a task cannot depend on itself")
        if self.timeout_seconds is not None and self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        object.__setattr__(self, "task_id", self.task_id.strip())
        object.__setattr__(self, "agent", self.agent.strip())
        object.__setattr__(self, "prompt", self.prompt.strip())
        object.__setattr__(self, "depends_on", tuple(dict.fromkeys(self.depends_on)))
        object.__setattr__(self, "context", MappingProxyType(dict(self.context)))


@dataclass(frozen=True, slots=True)
class AgentArtifact:
    path: str
    media_type: str = "text/plain"
    description: str = ""


@dataclass(frozen=True, slots=True)
class AgentOutput:
    content: str
    summary: str = ""
    artifacts: tuple[AgentArtifact, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "artifacts", tuple(self.artifacts))
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))


@dataclass(frozen=True, slots=True)
class AgentResult:
    task_id: str
    agent: str
    status: AgentRunStatus
    content: str = ""
    summary: str = ""
    artifacts: tuple[AgentArtifact, ...] = ()
    error: str | None = None
    duration_seconds: float = 0.0

    @property
    def ok(self) -> bool:
        return self.status is AgentRunStatus.SUCCEEDED


@dataclass(frozen=True, slots=True)
class AgentExecutionContext:
    shared_context: str
    dependency_results: Mapping[str, AgentResult] = field(default_factory=dict)
    run_metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "dependency_results", MappingProxyType(dict(self.dependency_results)))
        object.__setattr__(self, "run_metadata", MappingProxyType(dict(self.run_metadata)))


@dataclass(frozen=True, slots=True)
class AgentProgressEvent:
    task_id: str
    agent: str
    phase: str
    detail: str = ""


@dataclass(frozen=True, slots=True)
class AgentTeamResult:
    results: tuple[AgentResult, ...]

    def by_id(self) -> Mapping[str, AgentResult]:
        return MappingProxyType({result.task_id: result for result in self.results})

    @property
    def succeeded(self) -> bool:
        return all(result.ok for result in self.results)


class AgentRegistry:
    def __init__(self, specs: Iterable[AgentSpec] = ()) -> None:
        self._specs: dict[str, AgentSpec] = {}
        for spec in specs:
            self.register(spec)

    def register(self, spec: AgentSpec, *, replace: bool = False) -> None:
        if spec.name in self._specs and not replace:
            raise ValueError(f"agent {spec.name!r} is already registered")
        self._specs[spec.name] = spec

    def get(self, name: str) -> AgentSpec:
        try:
            return self._specs[name]
        except KeyError as exc:
            raise KeyError(f"unknown agent {name!r}") from exc

    def snapshot(self) -> dict[str, AgentSpec]:
        return dict(self._specs)


class AgentExecutor(Protocol):
    async def __call__(
        self,
        spec: AgentSpec,
        task: AgentTask,
        context: AgentExecutionContext,
    ) -> AgentOutput | str: ...


ProgressCallback: TypeAlias = Callable[[AgentProgressEvent], Awaitable[None] | None]


def validate_task_graph(tasks: Sequence[AgentTask]) -> None:
    ids = [task.task_id for task in tasks]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate task ids")
    known = set(ids)
    missing = sorted({d for task in tasks for d in task.depends_on if d not in known})
    if missing:
        raise ValueError(f"unknown task dependencies: {', '.join(missing)}")

    graph = {task.task_id: task.depends_on for task in tasks}
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(task_id: str) -> None:
        if task_id in visiting:
            raise ValueError(f"task dependency cycle at {task_id!r}")
        if task_id in visited:
            return
        visiting.add(task_id)
        for dependency in graph[task_id]:
            visit(dependency)
        visiting.remove(task_id)
        visited.add(task_id)

    for task_id in ids:
        visit(task_id)


class MultiAgentOrchestrator:
    """Run independent tasks in parallel and dependent tasks in waves."""

    def __init__(
        self,
        registry: AgentRegistry,
        executor: AgentExecutor,
        *,
        max_parallel: int = 4,
        fail_fast: bool = False,
    ) -> None:
        if max_parallel < 1:
            raise ValueError("max_parallel must be at least 1")
        self.registry = registry
        self.executor = executor
        self.max_parallel = max_parallel
        self.fail_fast = fail_fast

    async def run(
        self,
        tasks: Sequence[AgentTask],
        *,
        shared_context: str = "",
        run_metadata: Mapping[str, Any] | None = None,
        progress_callback: ProgressCallback | None = None,
    ) -> AgentTeamResult:
        tasks = tuple(tasks)
        validate_task_graph(tasks)
        specs = self.registry.snapshot()
        unknown = sorted({task.agent for task in tasks if task.agent not in specs})
        if unknown:
            raise KeyError(f"unknown task agents: {', '.join(unknown)}")

        task_map = {task.task_id: task for task in tasks}
        order = {task.task_id: index for index, task in enumerate(tasks)}
        pending = set(task_map)
        results: dict[str, AgentResult] = {}
        semaphore = asyncio.Semaphore(self.max_parallel)

        while pending:
            for task_id in list(pending):
                task = task_map[task_id]
                failed = [d for d in task.depends_on if d in results and not results[d].ok]
                if failed:
                    results[task_id] = AgentResult(
                        task_id,
                        task.agent,
                        AgentRunStatus.BLOCKED,
                        error=f"blocked by failed dependencies: {', '.join(failed)}",
                    )
                    pending.remove(task_id)
                    await self._emit(progress_callback, AgentProgressEvent(task_id, task.agent, "blocked"))

            if not pending:
                break

            ready = [
                task_map[task_id]
                for task_id in pending
                if all(d in results for d in task_map[task_id].depends_on)
            ]
            ready.sort(key=lambda task: order[task.task_id])
            if not ready:
                raise RuntimeError("no runnable tasks remain")

            slots: list[AgentResult | None] = [None] * len(ready)

            async def run_at(index: int, task: AgentTask) -> None:
                context = AgentExecutionContext(
                    shared_context,
                    {d: results[d] for d in task.depends_on},
                    run_metadata or {},
                )
                slots[index] = await self._run_one(
                    task,
                    specs[task.agent],
                    context,
                    semaphore,
                    progress_callback,
                )

            async with asyncio.TaskGroup() as group:
                for index, task in enumerate(ready):
                    group.create_task(run_at(index, task), name=f"ares-agent:{task.task_id}")

            batch = [result for result in slots if result is not None]
            for result in batch:
                results[result.task_id] = result
                pending.remove(result.task_id)

            if self.fail_fast and any(
                not result.ok and task_map[result.task_id].required for result in batch
            ):
                for task_id in sorted(pending, key=order.__getitem__):
                    task = task_map[task_id]
                    results[task_id] = AgentResult(
                        task_id,
                        task.agent,
                        AgentRunStatus.CANCELLED,
                        error="cancelled after required task failure",
                    )
                pending.clear()

        return AgentTeamResult(tuple(results[task.task_id] for task in tasks))

    async def _run_one(
        self,
        task: AgentTask,
        spec: AgentSpec,
        context: AgentExecutionContext,
        semaphore: asyncio.Semaphore,
        callback: ProgressCallback | None,
    ) -> AgentResult:
        started = time.perf_counter()
        await self._emit(callback, AgentProgressEvent(task.task_id, task.agent, "queued"))
        try:
            async with semaphore:
                await self._emit(callback, AgentProgressEvent(task.task_id, task.agent, "running", spec.description))
                timeout = task.timeout_seconds or spec.timeout_seconds
                async with asyncio.timeout(timeout):
                    raw = await self.executor(spec, task, context)
                output = raw if isinstance(raw, AgentOutput) else AgentOutput(str(raw))
                result = AgentResult(
                    task.task_id,
                    task.agent,
                    AgentRunStatus.SUCCEEDED,
                    output.content,
                    output.summary,
                    output.artifacts,
                    duration_seconds=time.perf_counter() - started,
                )
                await self._emit(callback, AgentProgressEvent(task.task_id, task.agent, "succeeded", output.summary))
                return result
        except TimeoutError:
            return AgentResult(
                task.task_id,
                task.agent,
                AgentRunStatus.TIMED_OUT,
                error=f"agent exceeded {task.timeout_seconds or spec.timeout_seconds:.2f}s timeout",
                duration_seconds=time.perf_counter() - started,
            )
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            return AgentResult(
                task.task_id,
                task.agent,
                AgentRunStatus.FAILED,
                error=f"{type(exc).__name__}: {exc}",
                duration_seconds=time.perf_counter() - started,
            )

    @staticmethod
    async def _emit(callback: ProgressCallback | None, event: AgentProgressEvent) -> None:
        if callback is None:
            return
        value = callback(event)
        if inspect.isawaitable(value):
            await value


READ_ONLY_RESEARCH_TOOLS = (
    "web_search", "fetch_url", "extract_document", "read_file", "search_files",
    "list_directory", "search_memory", "search_actions", "list_skills", "load_skill",
)
READ_ONLY_CODE_TOOLS = (
    "read_file", "search_files", "list_directory", "get_file_info", "glob_pattern",
    "show_file_with_line_numbers", "find_text", "compare_files",
)
CODE_MUTATION_TOOLS = READ_ONLY_CODE_TOOLS + (
    "write_file", "edit_file", "create_directory", "move_file", "batch_edit",
    "insert_line", "replace_lines", "delete_lines", "preview_diff", "backup_file",
    "undo_last_edit", "append_to_file", "prepend_to_file", "run_python", "run_shell",
)


def default_agent_specs() -> tuple[AgentSpec, ...]:
    return (
        AgentSpec("planner", "Creates a bounded dependency-aware plan.", "Plan minimal independent tasks."),
        AgentSpec("researcher", "Collects source-backed evidence.", "Prefer primary sources.", READ_ONLY_RESEARCH_TOOLS, timeout_seconds=180),
        AgentSpec("analyst", "Compares evidence and trade-offs.", "State assumptions and confidence.", READ_ONLY_CODE_TOOLS),
        AgentSpec("builder", "Implements an approved scoped change.", "Inspect, edit minimally, and test.", CODE_MUTATION_TOOLS, timeout_seconds=300, can_mutate=True),
        AgentSpec("reviewer", "Checks correctness, safety, and regressions.", "Review without mutating.", READ_ONLY_CODE_TOOLS),
        AgentSpec("synthesizer", "Combines specialist outputs.", "Preserve caveats and failed-task status."),
    )
