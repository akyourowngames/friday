"""Native, framework-neutral multi-agent orchestration for Ares."""

from __future__ import annotations

import asyncio
import inspect
import time
from datetime import UTC, datetime
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import Enum
from fnmatch import fnmatchcase
from types import MappingProxyType
from typing import Any, Protocol, TypeAlias


def _deep_freeze(value: Any) -> Any:
    """Recursively freeze JSON-like runtime truth at dataclass boundaries."""
    if isinstance(value, Mapping):
        return MappingProxyType({key: _deep_freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_deep_freeze(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(_deep_freeze(item) for item in value)
    return value


def mutable_metadata(value: Any) -> Any:
    """Return a detached JSON-compatible copy of recursively frozen metadata."""
    if isinstance(value, Mapping):
        return {key: mutable_metadata(item) for key, item in value.items()}
    if isinstance(value, (tuple, list, frozenset, set)):
        return [mutable_metadata(item) for item in value]
    return value


class AgentRunStatus(str, Enum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"


class AgentCapability(str, Enum):
    """Runtime-enforced authority granted to one specialist role.

    Tool allowlists decide *which* named tools are visible.  Capabilities are
    the independent security boundary that decides what those tools may do.
    Keeping the two checks separate prevents a configuration typo from turning
    a read-only role into an action-capable child.
    """

    FILESYSTEM_READ = "filesystem_read"
    FILESYSTEM_WRITE = "filesystem_write"
    CODE_EXECUTION = "code_execution"
    SHELL_EXECUTION = "shell_execution"
    BROWSER_READ = "browser_read"
    BROWSER_INTERACTION = "browser_interaction"
    DATABASE_READ = "database_read"
    DATABASE_WRITE = "database_write"
    COMMUNICATION = "communication"
    EXTERNAL_MUTATION = "external_mutation"
    DELEGATION = "delegation"


class ContextMode(str, Enum):
    FULL = "full"
    BOUNDED_SPECIALIST = "bounded_specialist"


class RetryableAgentError(RuntimeError):
    """An execution failure that is explicitly safe to retry.

    Adapters must only set ``retry_safe`` when no consequential tool started.
    The orchestrator never guesses retry safety from prose alone.
    """

    def __init__(
        self, message: str, *, retry_safe: bool = False, iterations: int = 0
    ) -> None:
        super().__init__(message)
        self.retry_safe = retry_safe
        self.iterations = max(0, int(iterations))


@dataclass(frozen=True, slots=True)
class AgentSpec:
    """Reusable specialist definition with a strict tool allowlist."""

    name: str
    description: str
    instructions: str
    allowed_tools: tuple[str, ...] = ()
    model: str | None = None
    max_iterations: int = 8
    max_output_tokens: int = 16000
    timeout_seconds: float = 120.0
    can_delegate: bool = False
    can_mutate: bool = False
    capabilities: tuple[AgentCapability | str, ...] = ()
    retry_limit: int = 0
    retry_backoff_seconds: float = 0.25
    fallback_models: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("agent name cannot be empty")
        if self.max_iterations < 1:
            raise ValueError("max_iterations must be at least 1")
        if self.max_output_tokens < 1:
            raise ValueError("max_output_tokens must be at least 1")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if self.retry_limit < 0:
            raise ValueError("retry_limit cannot be negative")
        if self.retry_backoff_seconds < 0:
            raise ValueError("retry_backoff_seconds cannot be negative")
        object.__setattr__(self, "name", self.name.strip())
        object.__setattr__(self, "allowed_tools", tuple(dict.fromkeys(self.allowed_tools)))
        normalized: list[AgentCapability] = []
        for value in self.capabilities:
            capability = value if isinstance(value, AgentCapability) else AgentCapability(str(value))
            if capability not in normalized:
                normalized.append(capability)
        if not normalized:
            # Compatibility for existing AgentSpec callers.  This deliberately
            # does not infer communication/external authority from can_mutate.
            normalized.extend((
                AgentCapability.FILESYSTEM_READ,
                AgentCapability.BROWSER_READ,
                AgentCapability.DATABASE_READ,
            ))
            if self.can_mutate:
                normalized.extend((
                    AgentCapability.FILESYSTEM_WRITE,
                    AgentCapability.CODE_EXECUTION,
                    AgentCapability.SHELL_EXECUTION,
                    AgentCapability.DATABASE_WRITE,
                ))
            if self.can_delegate:
                normalized.append(AgentCapability.DELEGATION)
        object.__setattr__(self, "capabilities", tuple(dict.fromkeys(normalized)))
        object.__setattr__(self, "fallback_models", tuple(dict.fromkeys(
            str(model).strip() for model in self.fallback_models if str(model).strip()
        )))

    def permits_tool(self, name: str) -> bool:
        return any(fnmatchcase(name, pattern) for pattern in self.allowed_tools)

    def permits_capability(self, capability: AgentCapability) -> bool:
        return capability in self.capabilities


def trusted_local_agent_spec(spec: AgentSpec) -> AgentSpec:
    """Return an operator-requested broad execution view of a specialist.

    This profile removes the *role-template* allowlist and capability gaps for
    local, operator-owned work. The reviewer remains read-only so independent
    patch review is meaningful. The profile deliberately does not grant
    recursive delegation: nested orchestration needs a parent-bound facade so
    it cannot escape the root run's budget or session ownership. Runtime
    authorization, action grants, confirmation ownership, and workspace
    containment remain mandatory at dispatch time.
    """
    if spec.name.casefold() == "reviewer":
        return spec
    return replace(
        spec,
        allowed_tools=("*",),
        can_mutate=True,
        capabilities=tuple(
            capability
            for capability in AgentCapability
            if capability is not AgentCapability.DELEGATION
        ),
    )


@dataclass(frozen=True, slots=True)
class AgentTask:
    task_id: str
    agent: str
    prompt: str
    depends_on: tuple[str, ...] = ()
    context: Mapping[str, Any] = field(default_factory=dict)
    timeout_seconds: float | None = None
    required: bool = True
    result_format: str = "text"
    allowed_context: tuple[str, ...] = ()
    allow_partial_dependencies: bool = False

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
        object.__setattr__(self, "context", _deep_freeze(self.context))
        object.__setattr__(self, "allowed_context", tuple(dict.fromkeys(
            str(value).strip() for value in self.allowed_context if str(value).strip()
        )))


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
        object.__setattr__(self, "metadata", _deep_freeze(self.metadata))


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
    run_id: str = ""
    parent_run_id: str = ""
    root_run_id: str = ""
    iterations: int = 0
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "artifacts", tuple(self.artifacts))
        object.__setattr__(self, "metadata", _deep_freeze(self.metadata))

    @property
    def ok(self) -> bool:
        return self.status is AgentRunStatus.SUCCEEDED


@dataclass(frozen=True, slots=True)
class AgentExecutionContext:
    shared_context: str
    dependency_results: Mapping[str, AgentResult] = field(default_factory=dict)
    run_metadata: Mapping[str, Any] = field(default_factory=dict)
    context_mode: ContextMode = ContextMode.BOUNDED_SPECIALIST
    allowed_context: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "dependency_results", MappingProxyType(dict(self.dependency_results)))
        object.__setattr__(self, "run_metadata", _deep_freeze(self.run_metadata))
        object.__setattr__(self, "allowed_context", tuple(dict.fromkeys(self.allowed_context)))


@dataclass(frozen=True, slots=True)
class AgentProgressEvent:
    task_id: str
    agent: str
    phase: str
    detail: str = ""
    event_type: str = "agent_progress"
    root_run_id: str = ""
    parent_run_id: str = ""
    run_id: str = ""
    session_id: str = ""
    parent_session_id: str = ""
    request_id: str = ""
    status: str = ""
    tool: str = ""
    timestamp: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", _deep_freeze(self.metadata))

    def as_dict(self) -> dict[str, Any]:
        return {
            "event_type": self.event_type,
            "root_run_id": self.root_run_id,
            "parent_run_id": self.parent_run_id,
            "run_id": self.run_id,
            "session_id": self.session_id,
            "parent_session_id": self.parent_session_id,
            "request_id": self.request_id,
            "task_id": self.task_id,
            "agent": self.agent,
            "phase": self.phase,
            "status": self.status or self.phase,
            "detail": self.detail,
            "tool": self.tool,
            "timestamp": self.timestamp,
            "metadata": mutable_metadata(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class ChildRunManifest:
    run_id: str
    task_id: str
    role: str
    session_id: str
    parent_session_id: str
    parent_run_id: str
    root_run_id: str
    status: str
    dependencies: tuple[str, ...] = ()
    started_at: str | None = None
    completed_at: str | None = None
    duration_seconds: float = 0.0
    tools: tuple[str, ...] = ()
    artifacts: tuple[AgentArtifact, ...] = ()
    error: str | None = None
    iterations: int = 0
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "dependencies", tuple(self.dependencies))
        object.__setattr__(self, "tools", tuple(self.tools))
        object.__setattr__(self, "artifacts", tuple(self.artifacts))
        object.__setattr__(self, "metadata", _deep_freeze(self.metadata))

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "task_id": self.task_id,
            "role": self.role,
            "session_id": self.session_id,
            "parent_session_id": self.parent_session_id,
            "parent_run_id": self.parent_run_id,
            "root_run_id": self.root_run_id,
            "status": self.status,
            "dependencies": list(self.dependencies),
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "duration_seconds": self.duration_seconds,
            "tools": list(self.tools),
            "artifacts": [
                {"path": item.path, "media_type": item.media_type, "description": item.description}
                for item in self.artifacts
            ],
            "error": self.error,
            "iterations": self.iterations,
            "metadata": mutable_metadata(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class AgentExecutionManifest:
    root_run_id: str
    session_id: str
    request_id: str
    child_runs: tuple[ChildRunManifest, ...]
    execution_waves: tuple[tuple[str, ...], ...]
    started_at: str
    completed_at: str | None
    status: str
    duration_seconds: float = 0.0
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "child_runs", tuple(self.child_runs))
        object.__setattr__(self, "execution_waves", tuple(tuple(wave) for wave in self.execution_waves))
        object.__setattr__(self, "metadata", _deep_freeze(self.metadata))

    @property
    def agent_count(self) -> int:
        return len(self.child_runs)

    def as_dict(self) -> dict[str, Any]:
        return {
            "root_run_id": self.root_run_id,
            "session_id": self.session_id,
            "request_id": self.request_id,
            "status": self.status,
            "agent_count": self.agent_count,
            "execution_waves": [list(wave) for wave in self.execution_waves],
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "duration_seconds": self.duration_seconds,
            "child_runs": [child.as_dict() for child in self.child_runs],
            "metadata": mutable_metadata(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class AgentTeamResult:
    results: tuple[AgentResult, ...]
    root_run_id: str = ""
    execution_waves: tuple[tuple[str, ...], ...] = ()
    manifest: AgentExecutionManifest | None = None

    def by_id(self) -> Mapping[str, AgentResult]:
        return MappingProxyType({result.task_id: result for result in self.results})

    @property
    def succeeded(self) -> bool:
        return all(result.ok for result in self.results)

    @property
    def status(self) -> str:
        if self.succeeded:
            return AgentRunStatus.SUCCEEDED.value
        if any(result.status is AgentRunStatus.FAILED for result in self.results):
            return AgentRunStatus.FAILED.value
        if any(result.status is AgentRunStatus.CANCELLED for result in self.results):
            return AgentRunStatus.CANCELLED.value
        if any(result.status is AgentRunStatus.TIMED_OUT for result in self.results):
            return AgentRunStatus.TIMED_OUT.value
        if any(result.status is AgentRunStatus.BLOCKED for result in self.results):
            return AgentRunStatus.BLOCKED.value
        return AgentRunStatus.FAILED.value

    def as_dict(self) -> dict[str, Any]:
        payload = {
            "root_run_id": self.root_run_id,
            "status": self.status,
            "execution_waves": [list(wave) for wave in self.execution_waves],
            "results": [
                {
                    "task_id": item.task_id,
                    "agent": item.agent,
                    "status": item.status.value,
                    "content": item.content,
                    "summary": item.summary,
                    "error": item.error,
                    "duration_seconds": item.duration_seconds,
                    "run_id": item.run_id,
                    "artifacts": [
                        {"path": artifact.path, "media_type": artifact.media_type, "description": artifact.description}
                        for artifact in item.artifacts
                    ],
                    "iterations": item.iterations,
                    "metadata": mutable_metadata(item.metadata),
                }
                for item in self.results
            ],
        }
        if self.manifest is not None:
            payload["manifest"] = self.manifest.as_dict()
            # Promote immutable manifest truth fields for simple consumers.
            payload.update({
                "session_id": self.manifest.session_id,
                "request_id": self.manifest.request_id,
                "agent_count": self.manifest.agent_count,
                "started_at": self.manifest.started_at,
                "completed_at": self.manifest.completed_at,
                "duration_seconds": self.manifest.duration_seconds,
            })
        return payload


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
        max_duration_seconds: float | None = None,
        initial_results: Mapping[str, AgentResult] | None = None,
    ) -> AgentTeamResult:
        tasks = tuple(tasks)
        validate_task_graph(tasks)
        specs = self.registry.snapshot()
        unknown = sorted({task.agent for task in tasks if task.agent not in specs})
        if unknown:
            raise KeyError(f"unknown task agents: {', '.join(unknown)}")

        task_map = {task.task_id: task for task in tasks}
        order = {task.task_id: index for index, task in enumerate(tasks)}
        results = dict(initial_results or {})
        unknown_results = sorted(set(results) - set(task_map))
        if unknown_results:
            raise ValueError(
                f"initial results reference unknown tasks: {', '.join(unknown_results)}"
            )
        for task_id, result in results.items():
            task = task_map[task_id]
            if result.task_id != task_id or result.agent != task.agent:
                raise ValueError(f"initial result identity mismatch for task {task_id!r}")
            if not result.ok:
                raise ValueError(
                    f"only successful checkpoint results may be reused ({task_id!r})"
                )
        pending = set(task_map) - set(results)
        semaphore = asyncio.Semaphore(self.max_parallel)
        waves: list[tuple[str, ...]] = []
        deadline = (
            time.monotonic() + max_duration_seconds
            if max_duration_seconds is not None and max_duration_seconds > 0
            else None
        )

        while pending:
            for task_id in list(pending):
                task = task_map[task_id]
                failed = [d for d in task.depends_on if d in results and not results[d].ok]
                if failed and not task.allow_partial_dependencies:
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
            waves.append(tuple(task.task_id for task in ready))

            slots: list[AgentResult | None] = [None] * len(ready)

            async def run_at(index: int, task: AgentTask) -> None:
                dependency_results = (
                    {d: results[d] for d in task.depends_on}
                    if "task_dependencies" in task.allowed_context
                    else {}
                )
                context = AgentExecutionContext(
                    shared_context,
                    dependency_results,
                    run_metadata or {},
                    ContextMode.BOUNDED_SPECIALIST,
                    task.allowed_context,
                )
                slots[index] = await self._run_one(
                    task,
                    specs[task.agent],
                    context,
                    semaphore,
                    progress_callback,
                    deadline,
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

        return AgentTeamResult(
            tuple(results[task.task_id] for task in tasks),
            root_run_id=str((run_metadata or {}).get("root_run_id") or ""),
            execution_waves=tuple(waves),
        )

    async def _run_one(
        self,
        task: AgentTask,
        spec: AgentSpec,
        context: AgentExecutionContext,
        semaphore: asyncio.Semaphore,
        callback: ProgressCallback | None,
        deadline: float | None = None,
    ) -> AgentResult:
        started = time.perf_counter()
        await self._emit(callback, AgentProgressEvent(task.task_id, task.agent, "queued"))
        try:
            async with semaphore:
                await self._emit(callback, AgentProgressEvent(task.task_id, task.agent, "running", spec.description))
                configured_timeout = task.timeout_seconds or spec.timeout_seconds
                raw: AgentOutput | str | None = None
                last_retry_error: RetryableAgentError | None = None
                consumed_iterations = 0
                for attempt in range(spec.retry_limit + 1):
                    remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
                    if remaining is not None and remaining <= 0:
                        raise TimeoutError
                    timeout = configured_timeout if remaining is None else min(configured_timeout, remaining)
                    remaining_iterations = spec.max_iterations - consumed_iterations
                    if remaining_iterations <= 0:
                        raise RuntimeError("specialist exhausted its aggregate retry iteration budget")
                    retry_context = AgentExecutionContext(
                        context.shared_context,
                        context.dependency_results,
                        {
                            **dict(context.run_metadata),
                            "attempt": attempt,
                            "remaining_iterations": remaining_iterations,
                            "fallback_model": spec.fallback_models[attempt - 1]
                            if attempt > 0 and attempt - 1 < len(spec.fallback_models)
                            else "",
                        },
                        context.context_mode,
                        context.allowed_context,
                    )
                    try:
                        async with asyncio.timeout(timeout):
                            raw = await self.executor(spec, task, retry_context)
                        last_retry_error = None
                        break
                    except RetryableAgentError as exc:
                        last_retry_error = exc
                        consumed_iterations += exc.iterations
                        if not exc.retry_safe or attempt >= spec.retry_limit:
                            raise
                        if spec.retry_backoff_seconds:
                            delay = spec.retry_backoff_seconds * (2 ** attempt)
                            if deadline is not None and time.monotonic() + delay >= deadline:
                                raise TimeoutError from exc
                            await asyncio.sleep(delay)
                if last_retry_error is not None:
                    raise last_retry_error
                output = raw if isinstance(raw, AgentOutput) else AgentOutput(str(raw or ""))
                total_iterations = consumed_iterations + int(output.metadata.get("iterations") or 0)
                if total_iterations > spec.max_iterations:
                    raise RuntimeError("specialist exceeded its aggregate retry iteration budget")
                result_metadata = mutable_metadata(output.metadata)
                result_metadata["iterations"] = total_iterations
                result = AgentResult(
                    task.task_id,
                    task.agent,
                    AgentRunStatus.SUCCEEDED,
                    output.content,
                    output.summary,
                    output.artifacts,
                    duration_seconds=time.perf_counter() - started,
                    run_id=str(output.metadata.get("run_id") or ""),
                    parent_run_id=str(output.metadata.get("parent_run_id") or ""),
                    root_run_id=str(output.metadata.get("root_run_id") or ""),
                    iterations=total_iterations,
                    metadata=result_metadata,
                )
                await self._emit(callback, AgentProgressEvent(
                    task.task_id,
                    task.agent,
                    "succeeded",
                    output.summary,
                    metadata={
                        "result_content": output.content,
                        "result_summary": output.summary,
                        "artifacts": [
                            {
                                "path": artifact.path,
                                "media_type": artifact.media_type,
                                "description": artifact.description,
                            }
                            for artifact in output.artifacts
                        ],
                        "iterations": total_iterations,
                        "result_metadata": result_metadata,
                    },
                ))
                return result
        except TimeoutError:
            result = AgentResult(
                task.task_id,
                task.agent,
                AgentRunStatus.TIMED_OUT,
                error=f"agent exceeded {task.timeout_seconds or spec.timeout_seconds:.2f}s timeout",
                duration_seconds=time.perf_counter() - started,
            )
            await self._emit(callback, AgentProgressEvent(task.task_id, task.agent, "timed_out", result.error or ""))
            return result
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            result = AgentResult(
                task.task_id,
                task.agent,
                AgentRunStatus.FAILED,
                error=f"{type(exc).__name__}: {exc}",
                duration_seconds=time.perf_counter() - started,
            )
            await self._emit(callback, AgentProgressEvent(task.task_id, task.agent, "failed", result.error or ""))
            return result

    @staticmethod
    async def _emit(callback: ProgressCallback | None, event: AgentProgressEvent) -> None:
        if callback is None:
            return
        value = callback(event)
        if inspect.isawaitable(value):
            await value


READ_ONLY_RESEARCH_TOOLS = (
    "web_search", "fetch_url", "download_online_file", "extract_document", "read_file", "search_files",
    "list_directory", "search_memory", "search_actions", "list_skills", "load_skill",
)
READ_ONLY_CODE_TOOLS = (
    "read_file", "search_files", "list_directory", "get_file_info", "glob_pattern",
    "show_file_with_line_numbers", "find_text", "compare_files",
)
CODE_MUTATION_TOOLS = READ_ONLY_CODE_TOOLS + (
    "write_file", "edit_file", "create_directory", "move_file", "batch_edit",
    "insert_line", "replace_lines", "delete_lines", "preview_diff", "backup_file",
    "undo_last_edit", "append_to_file", "prepend_to_file", "run_project_check",
)


def default_agent_specs() -> tuple[AgentSpec, ...]:
    return (
        AgentSpec("planner", "Decomposes complex work into bounded tasks and success criteria.", "Identify dependencies and success criteria. Do not mutate anything.", READ_ONLY_CODE_TOOLS),
        AgentSpec("researcher", "Collects evidence from the web, documentation, and read-only sources.", "Prefer primary sources and preserve source URLs.", READ_ONLY_RESEARCH_TOOLS, timeout_seconds=300),
        AgentSpec("analyst", "Inspects repository structure, integration points, risks, and tests.", "Remain read-only. State assumptions and affected components.", READ_ONLY_CODE_TOOLS),
        AgentSpec(
            "builder", "Implements approved scoped work and can run configured project checks.",
            "Inspect first, edit narrowly, use only configured project checks for verification, and respect every confirmation boundary.",
            CODE_MUTATION_TOOLS, timeout_seconds=300, can_mutate=True,
            capabilities=(
                AgentCapability.FILESYSTEM_READ, AgentCapability.FILESYSTEM_WRITE,
                AgentCapability.CODE_EXECUTION, AgentCapability.SHELL_EXECUTION,
                AgentCapability.DATABASE_READ,
            ),
        ),
        AgentSpec("reviewer", "Checks generated changes for correctness, regressions, security, and architecture.", "Remain read-only and report evidence-backed findings.", READ_ONLY_CODE_TOOLS),
        AgentSpec("synthesizer", "Combines specialist findings into a compact structured result.", "Resolve disagreements with evidence and preserve caveats."),
    )
