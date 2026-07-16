"""Deterministic planning helpers for upgraded delegation and skill routing.

Nothing in this module launches an agent, executes a tool, or mutates an
existing run.  The runtime can adopt these helpers incrementally while legacy
``AgentTask`` / ``AgentResult`` paths keep their current behavior.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


class DelegationUpgradeError(ValueError):
    """Raised when optional upgraded-delegation metadata is malformed."""


_WORD_RE = re.compile(r"[a-z0-9][a-z0-9-]{1,}")
_BUDGET_FIELDS = (
    "max_tasks",
    "max_parallel",
    "max_depth",
    "max_iterations",
    "max_output_tokens",
    "max_duration_seconds",
    "max_artifacts",
    "max_artifact_bytes",
)
_BUDGET_ALIASES = {
    "max_tasks_per_run": "max_tasks",
    "max_parallel_agents": "max_parallel",
    "max_total_iterations": "max_iterations",
    "max_total_tokens": "max_output_tokens",
    "max_total_duration_seconds": "max_duration_seconds",
}
_USAGE_BY_BUDGET = {
    "max_tasks": "task_count",
    "max_parallel": "active_agents",
    "max_depth": "depth",
    "max_iterations": "iterations",
    "max_output_tokens": "output_tokens",
    "max_duration_seconds": "duration_seconds",
    "max_artifacts": "artifact_count",
    "max_artifact_bytes": "artifact_bytes",
}
_DEFAULT_BUDGET_VALUES: dict[str, int | float] = {
    "max_tasks": 8,
    "max_parallel": 3,
    "max_depth": 1,
    "max_iterations": 64,
    "max_output_tokens": 64_000,
    "max_duration_seconds": 900.0,
    "max_artifacts": 32,
    "max_artifact_bytes": 64 * 1024 * 1024,
}
_HARD_BUDGET_VALUES: dict[str, int | float] = {
    "max_tasks": 128,
    "max_parallel": 32,
    "max_depth": 8,
    "max_iterations": 10_000,
    "max_output_tokens": 2_000_000,
    "max_duration_seconds": 86_400.0,
    "max_artifacts": 10_000,
    "max_artifact_bytes": 10 * 1024 * 1024 * 1024,
}
_INTEGER_BUDGET_FIELDS = frozenset(_BUDGET_FIELDS) - {"max_duration_seconds"}
_TERMINAL_STATUSES = frozenset({"succeeded", "failed", "timed_out", "blocked", "cancelled"})
_KNOWN_STATUSES = _TERMINAL_STATUSES | {"queued", "running"}
_STATUS_ALIASES = {"completed": "succeeded", "complete": "succeeded", "timeout": "timed_out"}


def _mapping(value: Any, *, name: str) -> Mapping[str, Any]:
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return value
    raise DelegationUpgradeError(f"{name} must be an object")


def _value_from(item: Any, name: str, default: Any = None) -> Any:
    if isinstance(item, Mapping):
        return item.get(name, default)
    return getattr(item, name, default)


def _as_strings(value: Any, *, field_name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        values = (value,)
    elif isinstance(value, Iterable) and not isinstance(value, (bytes, bytearray, Mapping)):
        values = tuple(value)
    else:
        raise DelegationUpgradeError(f"{field_name} must be a string or an array of strings")
    normalized: list[str] = []
    for raw in values:
        value_s = str(raw or "").strip()
        if not value_s:
            continue
        if value_s not in normalized:
            normalized.append(value_s)
    return tuple(normalized)


def _budget_value(value: Any, *, field_name: str, integer: bool, minimum: float = 0) -> int | float:
    if isinstance(value, bool):
        raise DelegationUpgradeError(f"{field_name} must be a number, not a boolean")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise DelegationUpgradeError(f"{field_name} must be numeric") from exc
    if not math.isfinite(number) or number < minimum:
        raise DelegationUpgradeError(f"{field_name} must be finite and at least {minimum:g}")
    if integer:
        if not number.is_integer():
            raise DelegationUpgradeError(f"{field_name} must be a whole number")
        return int(number)
    return number


def _canonical_budget_mapping(
    raw: Mapping[str, Any] | None,
    *,
    field_name: str,
    reject_unknown: bool,
    minimum: float,
) -> dict[str, int | float]:
    source = _mapping(raw, name=field_name)
    normalized: dict[str, int | float] = {}
    for raw_name, raw_value in source.items():
        key = _BUDGET_ALIASES.get(str(raw_name), str(raw_name))
        if key not in _BUDGET_FIELDS:
            if reject_unknown:
                raise DelegationUpgradeError(f"unknown {field_name} field: {raw_name}")
            continue
        value = _budget_value(
            raw_value,
            field_name=key,
            integer=key in _INTEGER_BUDGET_FIELDS,
            minimum=minimum,
        )
        previous = normalized.get(key)
        if previous is not None and previous != value:
            raise DelegationUpgradeError(f"conflicting {field_name} values for {key}")
        normalized[key] = value
    return normalized


@dataclass(frozen=True, slots=True)
class DelegationBudget:
    """Immutable resource ceilings for a single root delegation run."""

    max_tasks: int
    max_parallel: int
    max_depth: int
    max_iterations: int
    max_output_tokens: int
    max_duration_seconds: float
    max_artifacts: int
    max_artifact_bytes: int

    def as_dict(self) -> dict[str, int | float]:
        return {name: getattr(self, name) for name in _BUDGET_FIELDS}


@dataclass(frozen=True, slots=True)
class BudgetNormalization:
    budget: DelegationBudget
    capped: Mapping[str, tuple[int | float, int | float]] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "budget": self.budget.as_dict(),
            "capped": {key: list(value) for key, value in self.capped.items()},
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True, slots=True)
class BudgetDecision:
    allowed: bool
    usage: Mapping[str, int | float]
    remaining: Mapping[str, int | float]
    violations: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "usage": dict(self.usage),
            "remaining": dict(self.remaining),
            "violations": list(self.violations),
        }


def normalize_delegation_budget(
    raw: Mapping[str, Any] | None = None,
    *,
    hard_limits: Mapping[str, Any] | None = None,
    defaults: Mapping[str, Any] | None = None,
    reject_unknown: bool = True,
) -> BudgetNormalization:
    """Normalize aliases and cap an optional delegation budget at hard ceilings.

    Invalid values fail instead of being rounded.  Values above a ceiling are
    safely capped and reported, so a UI can explain why a requested budget was
    not granted without exposing the execution runtime to an oversized value.
    """

    default_values = dict(_DEFAULT_BUDGET_VALUES)
    default_values.update(
        _canonical_budget_mapping(
            defaults, field_name="budget defaults", reject_unknown=reject_unknown, minimum=0
        )
    )
    hard_values = dict(_HARD_BUDGET_VALUES)
    hard_values.update(
        _canonical_budget_mapping(
            hard_limits, field_name="hard budget", reject_unknown=reject_unknown, minimum=1
        )
    )
    requested = _canonical_budget_mapping(
        raw, field_name="delegation budget", reject_unknown=reject_unknown, minimum=0
    )
    values: dict[str, int | float] = {}
    capped: dict[str, tuple[int | float, int | float]] = {}
    warnings: list[str] = []
    for name in _BUDGET_FIELDS:
        value = requested.get(name, default_values[name])
        hard_limit = hard_values[name]
        if value > hard_limit:
            capped[name] = (value, hard_limit)
            warnings.append(f"{name} was capped at the configured hard limit")
            value = hard_limit
        values[name] = int(value) if name in _INTEGER_BUDGET_FIELDS else float(value)

    if values["max_tasks"] < 1 or values["max_parallel"] < 1 or values["max_depth"] < 1:
        raise DelegationUpgradeError("max_tasks, max_parallel, and max_depth must be at least 1")
    if values["max_parallel"] > values["max_tasks"]:
        capped["max_parallel"] = (values["max_parallel"], values["max_tasks"])
        values["max_parallel"] = values["max_tasks"]
        warnings.append("max_parallel was capped at max_tasks")
    return BudgetNormalization(DelegationBudget(**values), capped, tuple(dict.fromkeys(warnings)))


def enforce_delegation_budget(
    budget: DelegationBudget | BudgetNormalization | Mapping[str, Any],
    *,
    usage: Mapping[str, Any] | None = None,
    increment: Mapping[str, Any] | None = None,
) -> BudgetDecision:
    """Return a no-side-effect admission decision for a proposed resource use."""

    if isinstance(budget, BudgetNormalization):
        normalized_budget = budget.budget
    elif isinstance(budget, DelegationBudget):
        normalized_budget = budget
    else:
        normalized_budget = normalize_delegation_budget(budget).budget

    usage_raw = _mapping(usage, name="budget usage")
    increment_raw = _mapping(increment, name="budget increment")
    all_usage_keys = set(_USAGE_BY_BUDGET.values())
    for source_name, source in (("budget usage", usage_raw), ("budget increment", increment_raw)):
        unknown = set(source) - all_usage_keys
        if unknown:
            raise DelegationUpgradeError(f"unknown {source_name} field(s): {', '.join(sorted(map(str, unknown)))}")

    totals: dict[str, int | float] = {}
    remaining: dict[str, int | float] = {}
    violations: list[str] = []
    for limit_name, usage_name in _USAGE_BY_BUDGET.items():
        integer = limit_name in _INTEGER_BUDGET_FIELDS
        used = _budget_value(usage_raw.get(usage_name, 0), field_name=usage_name, integer=integer)
        added = _budget_value(increment_raw.get(usage_name, 0), field_name=usage_name, integer=integer)
        total = used + added
        limit = getattr(normalized_budget, limit_name)
        totals[usage_name] = int(total) if integer else float(total)
        remaining[usage_name] = max(0, limit - total)
        if total > limit:
            violations.append(f"{usage_name} would exceed {limit_name} ({total:g} > {limit:g})")
    return BudgetDecision(not violations, totals, remaining, tuple(violations))


def _normal_path(path_value: str, allowed_roots: Sequence[Path]) -> tuple[str | None, str | None]:
    raw = str(path_value or "").strip()
    if not raw:
        return None, "artifact path is required"
    candidate = Path(raw).expanduser()
    if not allowed_roots and ".." in candidate.parts:
        return None, "artifact path cannot traverse parents without an allowed root"
    try:
        if candidate.is_absolute():
            resolved = candidate.resolve()
            candidates = (resolved,)
        elif allowed_roots:
            candidates = tuple((root / candidate).resolve() for root in allowed_roots)
        else:
            candidates = (candidate.resolve(),)
    except OSError:
        return None, "artifact path could not be resolved"
    for resolved in candidates:
        if not allowed_roots or any(resolved == root or root in resolved.parents for root in allowed_roots):
            return str(resolved), None
    return None, "artifact path is outside the allowed roots"


@dataclass(frozen=True, slots=True)
class ContractValidation:
    valid: bool
    artifacts: tuple[Mapping[str, str], ...] = ()
    evidence: tuple[Mapping[str, str], ...] = ()
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "artifacts": [dict(item) for item in self.artifacts],
            "evidence": [dict(item) for item in self.evidence],
            "errors": list(self.errors),
            "warnings": list(self.warnings),
        }


def validate_evidence_artifact_contract(
    payload: Mapping[str, Any] | Any,
    *,
    require_evidence: bool = False,
    require_claims: bool = True,
    require_content: bool = False,
    allowed_artifact_roots: Sequence[str | Path] = (),
    artifact_must_exist: bool = False,
    max_artifacts: int = 32,
) -> ContractValidation:
    """Validate output evidence and artifacts before a root trusts a child result.

    The result is declarative rather than an exception for ordinary contract
    failures, allowing the root to request a repair from the child.  Malformed
    validator configuration still raises ``DelegationUpgradeError``.
    """

    if max_artifacts < 0:
        raise DelegationUpgradeError("max_artifacts cannot be negative")
    raw = dict(payload) if isinstance(payload, Mapping) else {
        key: _value_from(payload, key) for key in ("content", "summary", "artifacts", "evidence", "metadata")
    }
    errors: list[str] = []
    warnings: list[str] = []
    if require_content and not str(raw.get("content") or raw.get("summary") or "").strip():
        errors.append("result must include content or summary")
    metadata = raw.get("metadata")
    metadata_map = metadata if isinstance(metadata, Mapping) else {}
    raw_artifacts = raw.get("artifacts") or ()
    raw_evidence = raw.get("evidence", metadata_map.get("evidence", ())) or ()
    if not isinstance(raw_artifacts, (list, tuple)):
        errors.append("artifacts must be an array")
        raw_artifacts = ()
    if not isinstance(raw_evidence, (list, tuple)):
        errors.append("evidence must be an array")
        raw_evidence = ()
    if len(raw_artifacts) > max_artifacts:
        errors.append(f"artifact count exceeds max_artifacts ({len(raw_artifacts)} > {max_artifacts})")

    roots = tuple(Path(root).expanduser().resolve() for root in allowed_artifact_roots)
    artifacts: list[Mapping[str, str]] = []
    artifact_paths: set[str] = set()
    for index, artifact in enumerate(raw_artifacts[:max_artifacts]):
        path = _value_from(artifact, "path", "")
        media_type = str(_value_from(artifact, "media_type", "text/plain") or "").strip()
        description = str(_value_from(artifact, "description", "") or "").strip()
        normalized_path, path_error = _normal_path(str(path), roots)
        if path_error:
            errors.append(f"artifact[{index}]: {path_error}")
            continue
        if "/" not in media_type or media_type.startswith("/") or media_type.endswith("/"):
            errors.append(f"artifact[{index}]: media_type must be a MIME type")
            continue
        if normalized_path in artifact_paths:
            errors.append(f"artifact[{index}]: duplicate artifact path")
            continue
        if artifact_must_exist and not Path(normalized_path).is_file():
            errors.append(f"artifact[{index}]: file does not exist")
            continue
        artifact_paths.add(normalized_path)
        artifacts.append({"path": normalized_path, "media_type": media_type, "description": description})

    evidence: list[Mapping[str, str]] = []
    for index, item in enumerate(raw_evidence):
        if not isinstance(item, Mapping):
            errors.append(f"evidence[{index}] must be an object")
            continue
        claim = str(item.get("claim") or item.get("summary") or "").strip()
        source = str(item.get("source") or item.get("url") or item.get("artifact") or "").strip()
        if require_claims and not claim:
            errors.append(f"evidence[{index}] requires a claim")
            continue
        if not source:
            errors.append(f"evidence[{index}] requires source, url, or artifact")
            continue
        artifact_ref = str(item.get("artifact") or "").strip()
        if artifact_ref and artifact_paths and artifact_ref not in artifact_paths:
            warnings.append(f"evidence[{index}] references an artifact outside this result")
        evidence.append({"claim": claim, "source": source})
    if require_evidence and not evidence:
        errors.append("at least one evidence item is required")
    return ContractValidation(not errors, tuple(artifacts), tuple(evidence), tuple(errors), tuple(warnings))


@dataclass(frozen=True, slots=True)
class TaskNode:
    task_id: str
    agent: str
    prompt: str
    depends_on: tuple[str, ...] = ()
    resource_keys: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "agent": self.agent,
            "prompt": self.prompt,
            "depends_on": list(self.depends_on),
            "resource_keys": list(self.resource_keys),
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class TaskConflict:
    first_task_id: str
    second_task_id: str
    resources: tuple[str, ...]
    resolution: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "first_task_id": self.first_task_id,
            "second_task_id": self.second_task_id,
            "resources": list(self.resources),
            "resolution": self.resolution,
        }


@dataclass(frozen=True, slots=True)
class TaskGraphPlan:
    tasks: tuple[TaskNode, ...]
    dependencies: Mapping[str, tuple[str, ...]]
    waves: tuple[tuple[str, ...], ...]
    conflicts: tuple[TaskConflict, ...] = ()
    synthesis_task: Mapping[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "tasks": [task.as_dict() for task in self.tasks],
            "dependencies": {key: list(value) for key, value in self.dependencies.items()},
            "waves": [list(wave) for wave in self.waves],
            "conflicts": [conflict.as_dict() for conflict in self.conflicts],
            "synthesis_task": dict(self.synthesis_task) if self.synthesis_task else None,
        }


def _normalize_task(item: Mapping[str, Any] | Any) -> TaskNode:
    task_id = str(_value_from(item, "task_id", _value_from(item, "id", "")) or "").strip()
    if not task_id:
        raise DelegationUpgradeError("each task requires task_id")
    agent = str(_value_from(item, "agent", _value_from(item, "role", "")) or "").strip()
    prompt = str(_value_from(item, "prompt", _value_from(item, "task", "")) or "").strip()
    dependencies = _as_strings(
        _value_from(item, "depends_on", _value_from(item, "depends", ())), field_name=f"task {task_id}.depends_on"
    )
    resources: list[str] = []
    for name in ("resource_keys", "write_targets", "exclusive_resources"):
        for key in _as_strings(_value_from(item, name, ()), field_name=f"task {task_id}.{name}"):
            normalized = key.casefold()
            if normalized not in resources:
                resources.append(normalized)
    metadata = _value_from(item, "metadata", {})
    if not isinstance(metadata, Mapping):
        raise DelegationUpgradeError(f"task {task_id}.metadata must be an object")
    return TaskNode(task_id, agent, prompt, dependencies, tuple(resources), dict(metadata))


def _has_dependency(graph: Mapping[str, set[str]], task_id: str, candidate: str) -> bool:
    pending = list(graph[task_id])
    seen: set[str] = set()
    while pending:
        current = pending.pop()
        if current == candidate:
            return True
        if current not in seen:
            seen.add(current)
            pending.extend(graph[current])
    return False


def _waves(graph: Mapping[str, set[str]], order: Mapping[str, int]) -> tuple[tuple[str, ...], ...]:
    pending = set(graph)
    complete: set[str] = set()
    result: list[tuple[str, ...]] = []
    while pending:
        ready = sorted((task_id for task_id in pending if graph[task_id] <= complete), key=order.__getitem__)
        if not ready:
            raise DelegationUpgradeError("task dependency graph has a cycle")
        result.append(tuple(ready))
        complete.update(ready)
        pending.difference_update(ready)
    return tuple(result)


def plan_delegation_dag(
    tasks: Sequence[Mapping[str, Any] | Any],
    *,
    synthesize: bool = True,
    synthesis_task_id: str = "synthesize",
    synthesis_agent: str = "synthesizer",
    synthesis_prompt: str = "Synthesize the independent specialist results into one evidence-backed answer.",
) -> TaskGraphPlan:
    """Validate tasks, serialize conflicting resources, and optionally add synthesis.

    Conflicting independent resource writers are ordered deterministically by
    input order.  This avoids a hidden shared-workspace race while retaining
    all independent work in the same plan.
    """

    nodes = tuple(_normalize_task(task) for task in tasks)
    if not nodes:
        raise DelegationUpgradeError("at least one task is required")
    ids = [task.task_id for task in nodes]
    if len(ids) != len(set(ids)):
        raise DelegationUpgradeError("duplicate task ids")
    graph = {task.task_id: set(task.depends_on) for task in nodes}
    unknown = sorted({dependency for deps in graph.values() for dependency in deps if dependency not in graph})
    if unknown:
        raise DelegationUpgradeError(f"unknown task dependencies: {', '.join(unknown)}")
    if any(task_id in deps for task_id, deps in graph.items()):
        raise DelegationUpgradeError("a task cannot depend on itself")
    order = {task_id: index for index, task_id in enumerate(ids)}
    _waves(graph, order)

    conflicts: list[TaskConflict] = []
    for index, first in enumerate(nodes):
        for second in nodes[index + 1:]:
            resources = tuple(sorted(set(first.resource_keys) & set(second.resource_keys)))
            if not resources:
                continue
            if _has_dependency(graph, first.task_id, second.task_id) or _has_dependency(graph, second.task_id, first.task_id):
                conflicts.append(TaskConflict(first.task_id, second.task_id, resources, "already_ordered"))
                continue
            graph[second.task_id].add(first.task_id)
            conflicts.append(TaskConflict(first.task_id, second.task_id, resources, "serialized"))
    planned_waves = _waves(graph, order)

    synthesis_task: Mapping[str, Any] | None = None
    if synthesize:
        dependency_ids = {dependency for dependencies in graph.values() for dependency in dependencies}
        terminals = tuple(task_id for task_id in ids if task_id not in dependency_ids)
        if len(terminals) > 1:
            synthesis_id = str(synthesis_task_id or "").strip()
            if not synthesis_id:
                raise DelegationUpgradeError("synthesis_task_id cannot be empty")
            if synthesis_id in graph:
                raise DelegationUpgradeError("synthesis_task_id collides with a task id")
            synthesis_task = {
                "task_id": synthesis_id,
                "agent": str(synthesis_agent or "synthesizer").strip() or "synthesizer",
                "prompt": str(synthesis_prompt or "").strip(),
                "depends_on": list(terminals),
                "allowed_context": ["task_dependencies"],
                "result_format": "evidence_bundle",
            }
            graph[synthesis_id] = set(terminals)
            order[synthesis_id] = len(order)
            planned_waves = _waves(graph, order)
            nodes = (*nodes, TaskNode(
                synthesis_id,
                str(synthesis_task["agent"]),
                str(synthesis_task["prompt"]),
                terminals,
                (),
                {"generated": True, "result_format": "evidence_bundle"},
            ))
    return TaskGraphPlan(
        nodes,
        {task_id: tuple(sorted(dependencies, key=order.__getitem__)) for task_id, dependencies in graph.items()},
        planned_waves,
        tuple(conflicts),
        synthesis_task,
    )


@dataclass(frozen=True, slots=True)
class DelegationCheckpoint:
    statuses: Mapping[str, str]
    completed: tuple[str, ...]
    remaining: tuple[str, ...]
    source: str = "projection"

    def as_dict(self) -> dict[str, Any]:
        return {
            "statuses": dict(self.statuses),
            "completed": list(self.completed),
            "remaining": list(self.remaining),
            "source": self.source,
        }


@dataclass(frozen=True, slots=True)
class ProgressProjection:
    total: int
    statuses: Mapping[str, str]
    completed: tuple[str, ...]
    failed: tuple[str, ...]
    blocked: tuple[str, ...]
    running: tuple[str, ...]
    ready: tuple[str, ...]
    remaining: tuple[str, ...]
    completion_ratio: float
    success_ratio: float
    current_wave: int | None
    checkpoint: DelegationCheckpoint

    def as_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "statuses": dict(self.statuses),
            "completed": list(self.completed),
            "failed": list(self.failed),
            "blocked": list(self.blocked),
            "running": list(self.running),
            "ready": list(self.ready),
            "remaining": list(self.remaining),
            "completion_ratio": self.completion_ratio,
            "success_ratio": self.success_ratio,
            "current_wave": self.current_wave,
            "checkpoint": self.checkpoint.as_dict(),
        }


def _status(value: Any) -> str:
    normalized = _STATUS_ALIASES.get(str(value or "queued").strip().casefold(), str(value or "queued").strip().casefold())
    if normalized not in _KNOWN_STATUSES:
        raise DelegationUpgradeError(f"unknown task status: {value}")
    return normalized


def normalize_delegation_checkpoint(
    checkpoint: Mapping[str, Any] | None,
    task_ids: Sequence[str],
) -> DelegationCheckpoint:
    """Normalize persisted checkpoint truth without attempting a resume."""

    known = tuple(dict.fromkeys(str(task_id).strip() for task_id in task_ids if str(task_id).strip()))
    raw = _mapping(checkpoint, name="checkpoint")
    raw_statuses = raw.get("statuses", {})
    if raw_statuses is None:
        raw_statuses = {}
    if not isinstance(raw_statuses, Mapping):
        raise DelegationUpgradeError("checkpoint.statuses must be an object")
    statuses = {task_id: "queued" for task_id in known}
    unknown = sorted(set(map(str, raw_statuses)) - set(known))
    if unknown:
        raise DelegationUpgradeError(f"checkpoint references unknown tasks: {', '.join(unknown)}")
    for task_id, value in raw_statuses.items():
        statuses[str(task_id)] = _status(value)
    for task_id in _as_strings(raw.get("completed", ()), field_name="checkpoint.completed"):
        if task_id not in statuses:
            raise DelegationUpgradeError(f"checkpoint references unknown task: {task_id}")
        statuses[task_id] = "succeeded"
    completed = tuple(task_id for task_id in known if statuses[task_id] == "succeeded")
    remaining = tuple(task_id for task_id in known if task_id not in completed)
    return DelegationCheckpoint(statuses, completed, remaining, str(raw.get("source") or "checkpoint"))


def project_delegation_progress(
    plan: TaskGraphPlan | Sequence[Mapping[str, Any] | Any],
    *,
    events: Sequence[Mapping[str, Any] | Any] = (),
    checkpoint: Mapping[str, Any] | DelegationCheckpoint | None = None,
) -> ProgressProjection:
    """Project UI-safe progress/checkpoint state from immutable plan plus events."""

    graph_plan = plan if isinstance(plan, TaskGraphPlan) else plan_delegation_dag(plan, synthesize=False)
    task_ids = tuple(task.task_id for task in graph_plan.tasks)
    if isinstance(checkpoint, DelegationCheckpoint):
        statuses = dict(checkpoint.statuses)
        if set(statuses) != set(task_ids):
            raise DelegationUpgradeError("checkpoint task set does not match the plan")
    else:
        statuses = dict(normalize_delegation_checkpoint(checkpoint, task_ids).statuses)
    for event in events:
        task_id = str(_value_from(event, "task_id", "") or "").strip()
        if not task_id:
            continue
        if task_id not in statuses:
            raise DelegationUpgradeError(f"progress event references unknown task: {task_id}")
        statuses[task_id] = _status(_value_from(event, "status", _value_from(event, "phase", "queued")))

    completed = tuple(task_id for task_id in task_ids if statuses[task_id] == "succeeded")
    failed = tuple(task_id for task_id in task_ids if statuses[task_id] in {"failed", "timed_out", "cancelled"})
    blocked = tuple(task_id for task_id in task_ids if statuses[task_id] == "blocked")
    running = tuple(task_id for task_id in task_ids if statuses[task_id] == "running")
    ready = tuple(
        task_id for task_id in task_ids
        if statuses[task_id] == "queued" and all(statuses[dependency] == "succeeded" for dependency in graph_plan.dependencies[task_id])
    )
    remaining = tuple(task_id for task_id in task_ids if task_id not in completed)
    terminal_count = sum(status in _TERMINAL_STATUSES for status in statuses.values())
    current_wave = next(
        (index for index, wave in enumerate(graph_plan.waves) if any(statuses[task_id] not in _TERMINAL_STATUSES for task_id in wave)),
        None,
    )
    normalized_checkpoint = DelegationCheckpoint(statuses, completed, remaining, "projection")
    total = len(task_ids)
    return ProgressProjection(
        total,
        statuses,
        completed,
        failed,
        blocked,
        running,
        ready,
        remaining,
        terminal_count / total if total else 1.0,
        len(completed) / total if total else 1.0,
        current_wave,
        normalized_checkpoint,
    )


@dataclass(frozen=True, slots=True)
class SkillDescriptor:
    name: str
    description: str = ""
    category: str = "general"
    dependencies: tuple[str, ...] = ()
    provides: tuple[str, ...] = ()
    overlaps: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SkillRecommendation:
    name: str
    score: int
    reasons: tuple[str, ...]
    dependencies: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "score": self.score,
            "reasons": list(self.reasons),
            "dependencies": list(self.dependencies),
        }


@dataclass(frozen=True, slots=True)
class SkillRanking:
    selected: tuple[SkillRecommendation, ...]
    candidates: tuple[SkillRecommendation, ...]
    suppressed: Mapping[str, str]
    warnings: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "selected": [item.as_dict() for item in self.selected],
            "candidates": [item.as_dict() for item in self.candidates],
            "suppressed": dict(self.suppressed),
            "warnings": list(self.warnings),
        }


def _skill_name(value: Any) -> str:
    return str(value or "").strip().casefold().replace("_", "-").replace(" ", "-")


def _skill_values(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    raw_values = [value] if isinstance(value, str) else value if isinstance(value, Iterable) and not isinstance(value, Mapping) else ()
    return tuple(dict.fromkeys(name for item in raw_values if (name := _skill_name(item))))


def _normalize_skill(item: Mapping[str, Any] | Any) -> SkillDescriptor:
    name = _skill_name(_value_from(item, "name", ""))
    if not name:
        raise DelegationUpgradeError("skill requires a name")
    metadata = _value_from(item, "metadata", {})
    if not isinstance(metadata, Mapping):
        raise DelegationUpgradeError(f"skill {name}.metadata must be an object")
    dependencies: list[str] = []
    for key in ("dependencies", "requires", "requires-skills", "requires_skills", "requires-primary", "requires_primary"):
        for dependency in _skill_values(metadata.get(key, _value_from(item, key, ()) )):
            if dependency != name and dependency not in dependencies:
                dependencies.append(dependency)
    provides: list[str] = []
    for key in ("provides", "capabilities", "domains"):
        for capability in _skill_values(metadata.get(key, _value_from(item, key, ()) )):
            if capability not in provides:
                provides.append(capability)
    overlaps: list[str] = []
    for key in ("overlaps", "overlap_with", "conflicts", "conflicts_with"):
        for peer in _skill_values(metadata.get(key, _value_from(item, key, ()) )):
            if peer != name and peer not in overlaps:
                overlaps.append(peer)
    return SkillDescriptor(
        name,
        str(_value_from(item, "description", "") or ""),
        str(_value_from(item, "category", "general") or "general"),
        tuple(dependencies),
        tuple(provides),
        tuple(overlaps),
    )


def _tokens(value: str) -> set[str]:
    return set(_WORD_RE.findall(value.casefold()))


def _skill_score(skill: SkillDescriptor, query: str, query_tokens: set[str], explicit: bool) -> tuple[int, tuple[str, ...]]:
    name_text = skill.name.replace("-", " ")
    name_tokens = _tokens(name_text)
    description_tokens = _tokens(skill.description)
    category_tokens = _tokens(skill.category)
    provides_tokens = set(skill.provides)
    name_hits = query_tokens & name_tokens
    description_hits = query_tokens & description_tokens
    category_hits = query_tokens & category_tokens
    provides_hits = query_tokens & provides_tokens
    score = 10 * len(name_hits) + 3 * len(description_hits) + 2 * len(category_hits) + 4 * len(provides_hits)
    reasons: list[str] = []
    if explicit or skill.name in query.casefold() or name_text in query.casefold():
        score += 30
        reasons.append("explicitly named")
    if name_hits:
        reasons.append("name overlap: " + ", ".join(sorted(name_hits)))
    if description_hits:
        reasons.append("description overlap: " + ", ".join(sorted(description_hits)[:3]))
    if provides_hits:
        reasons.append("capability overlap: " + ", ".join(sorted(provides_hits)))
    return score, tuple(reasons)


def _skills_overlap(first: SkillDescriptor, second: SkillDescriptor) -> bool:
    if second.name in first.overlaps or first.name in second.overlaps:
        return True
    first_provides = set(first.provides)
    second_provides = set(second.provides)
    if first_provides and second_provides and first_provides & second_provides:
        return True
    first_terms = _tokens(first.name.replace("-", " "))
    second_terms = _tokens(second.name.replace("-", " "))
    return bool(first_terms and second_terms and len(first_terms & second_terms) / min(len(first_terms), len(second_terms)) >= 0.8)


def rank_skills_for_delegation(
    query: str,
    skills: Sequence[Mapping[str, Any] | Any],
    *,
    limit: int = 3,
    explicitly_requested: Sequence[str] = (),
) -> SkillRanking:
    """Rank focused skills, expand required dependencies, and suppress overlaps."""

    if limit < 0:
        raise DelegationUpgradeError("limit cannot be negative")
    catalog_items = tuple(_normalize_skill(skill) for skill in skills)
    catalog = {skill.name: skill for skill in catalog_items}
    if len(catalog) != len(catalog_items):
        raise DelegationUpgradeError("skill names must be unique")
    query_text = str(query or "")
    query_tokens = _tokens(query_text)
    explicit = set(_skill_values(explicitly_requested))
    scored: list[tuple[int, SkillDescriptor, tuple[str, ...]]] = []
    for skill in catalog_items:
        score, reasons = _skill_score(skill, query_text, query_tokens, skill.name in explicit)
        if score > 0 or skill.name in explicit:
            scored.append((score, skill, reasons or ("direct task match",)))
    scored.sort(key=lambda item: (-item[0], item[1].name))

    selected_roots: list[tuple[int, SkillDescriptor, tuple[str, ...]]] = []
    suppressed: dict[str, str] = {}
    for score, skill, reasons in scored:
        if len(selected_roots) >= limit:
            suppressed.setdefault(skill.name, "outside requested ranking limit")
            continue
        overlap = next((chosen for _score, chosen, _reasons in selected_roots if _skills_overlap(skill, chosen)), None)
        if overlap is not None and skill.name not in explicit:
            suppressed[skill.name] = f"overlaps with higher-ranked skill {overlap.name}"
            continue
        selected_roots.append((score, skill, reasons))

    selected: list[SkillRecommendation] = []
    warnings: list[str] = []
    visiting: set[str] = set()
    visited: set[str] = set()

    def add_skill(skill: SkillDescriptor, score: int, reasons: tuple[str, ...], *, required_by: str = "") -> None:
        if skill.name in visited:
            return
        if skill.name in visiting:
            warnings.append(f"skill dependency cycle at {skill.name}")
            return
        visiting.add(skill.name)
        for dependency_name in skill.dependencies:
            dependency = catalog.get(dependency_name)
            if dependency is None:
                warnings.append(f"skill {skill.name} requires missing skill {dependency_name}")
                continue
            add_skill(dependency, 0, (f"required by {skill.name}",), required_by=skill.name)
        visiting.remove(skill.name)
        visited.add(skill.name)
        dependency_reason = reasons if not required_by else tuple(dict.fromkeys((*reasons, f"required by {required_by}")))
        selected.append(SkillRecommendation(skill.name, score, dependency_reason, skill.dependencies))

    for score, skill, reasons in selected_roots:
        add_skill(skill, score, reasons)
    candidates = tuple(SkillRecommendation(skill.name, score, reasons, skill.dependencies) for score, skill, reasons in scored)
    return SkillRanking(tuple(selected), candidates, suppressed, tuple(dict.fromkeys(warnings)))
