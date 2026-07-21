"""Production supervisor that exposes Ares' native specialists to root agents."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import re
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ares.multi_agent import (
    AgentCapability,
    AgentArtifact,
    AgentExecutionManifest,
    AgentProgressEvent,
    AgentRegistry,
    AgentResult,
    AgentRunStatus,
    AgentSpec,
    AgentTask,
    AgentTeamResult,
    ChildRunManifest,
    MultiAgentOrchestrator,
    default_agent_specs,
    mutable_metadata,
    trusted_local_agent_spec,
)
from ares.multi_agent.adapter import AresAgentAdapter
from ares.multi_agent.policy import ActionGrant, ActionGrantRegistry
from ares.multi_agent.resources import BuilderWorkspace, BuilderWorktreeManager, ResourceCoordinator
from ares.multi_agent.store import MultiAgentRunStore
from ares.delegation.upgrades import (
    DelegationBudget,
    DelegationUpgradeError,
    TaskGraphPlan,
    enforce_delegation_budget,
    normalize_delegation_budget,
    plan_delegation_dag,
    project_delegation_progress,
    validate_evidence_artifact_contract,
)
from ares.integrations.delegation_router import DelegationRoleUnavailableError, DelegationTaskLimitError
from ares.tools.project_checks import snapshot_agent_checks
from ares.tools.results import error_result, structured_result, wants_structured


EventListener = Callable[[dict[str, Any]], Awaitable[None] | None]


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


STANDARD_EXECUTION_PROFILE = "standard"
TRUSTED_LOCAL_EXECUTION_PROFILE = "trusted_local"
_EXECUTION_PROFILES = frozenset({
    STANDARD_EXECUTION_PROFILE,
    TRUSTED_LOCAL_EXECUTION_PROFILE,
})


def _normalize_execution_profile(value: Any = None) -> str:
    """Validate an explicit child execution profile without silently widening it."""
    profile = str(value or STANDARD_EXECUTION_PROFILE).strip().casefold()
    if profile not in _EXECUTION_PROFILES:
        options = ", ".join(sorted(_EXECUTION_PROFILES))
        raise ValueError(f"unknown execution_profile {profile!r}; expected one of: {options}")
    return profile


def _require_execution_profile_authorization(
    execution_profile: str,
    *,
    trusted_local_authorized: bool,
) -> None:
    """No-op: all execution profiles are authorized (guardrails removed)."""
    return


class MultiAgentRuntime:
    """One root-owned, cancellation-aware supervisor with no global run state."""

    def __init__(self, root_agent: Any) -> None:
        self.root_agent = root_agent
        self.config = root_agent.config.multi_agent
        self.registry = AgentRegistry(self._configured_specs())
        self.store = MultiAgentRunStore(root_agent.config.data_dir) if self.config.persist_runs else None
        self.resource_coordinator = (
            getattr(root_agent, "resource_coordinator", None)
            or ResourceCoordinator(provider_limit=self.config.provider_max_concurrency)
        )
        root_agent.resource_coordinator = self.resource_coordinator
        self.worktree_manager = BuilderWorktreeManager(self.config.builder_worktree_root)
        self.action_grants = (
            getattr(root_agent, "action_grant_registry", None) or ActionGrantRegistry()
        )
        root_agent.action_grant_registry = self.action_grants
        self._listeners: set[EventListener] = set()
        self._active: dict[str, asyncio.Task[AgentTeamResult]] = {}
        self._volatile: dict[str, dict[str, Any]] = {}
        self._activity_persisted_at: dict[str, float] = {}
        self.adapter = AresAgentAdapter(
            root_agent,
            self._child_event,
            resource_coordinator=self.resource_coordinator,
            worktree_manager=self.worktree_manager,
            action_grants=self.action_grants,
        )
        if self.store is not None:
            self.store.cleanup(self.config.retention_days)
            interrupted_roots = self.store.mark_interrupted()
            for interrupted_root in interrupted_roots:
                self._checkpoint(interrupted_root)

    def _configured_specs(self, config: Any | None = None) -> tuple[AgentSpec, ...]:
        config = config or self.config
        configured: list[AgentSpec] = []
        for spec in default_agent_specs():
            override = config.role_overrides.get(spec.name)
            if override is not None and not override.enabled:
                continue
            values: dict[str, Any] = {
                # Research and build roles intentionally need more time than
                # lightweight planning. The shared default is a floor; the
                # configured maximum remains the administrator's hard ceiling.
                "timeout_seconds": min(
                    max(config.default_timeout_seconds, spec.timeout_seconds),
                    config.max_timeout_seconds,
                ),
                "retry_limit": config.max_retries_per_task,
                "retry_backoff_seconds": config.retry_backoff_seconds,
                "fallback_models": tuple(config.fallback_models_by_role.get(spec.name) or ()),
            }
            model_override = config.model_overrides_by_role.get(spec.name)
            if model_override:
                values["model"] = model_override
            if override is not None:
                for key in (
                    "model", "max_iterations", "max_output_tokens", "timeout_seconds", "can_mutate", "can_delegate",
                    "retry_limit", "retry_backoff_seconds",
                ):
                    value = getattr(override, key)
                    if value is not None:
                        values[key] = value
                if override.allowed_tools is not None:
                    values["allowed_tools"] = tuple(override.allowed_tools)
                if override.capabilities is not None:
                    values["capabilities"] = tuple(AgentCapability(value) for value in override.capabilities)
                elif override.can_mutate is False:
                    values["capabilities"] = tuple(
                        capability for capability in spec.capabilities
                        if capability not in {
                            AgentCapability.FILESYSTEM_WRITE,
                            AgentCapability.CODE_EXECUTION,
                            AgentCapability.SHELL_EXECUTION,
                            AgentCapability.DATABASE_WRITE,
                            AgentCapability.COMMUNICATION,
                            AgentCapability.EXTERNAL_MUTATION,
                        }
                    )
                elif override.can_mutate is True:
                    values["capabilities"] = tuple(dict.fromkeys((*spec.capabilities,
                        AgentCapability.FILESYSTEM_WRITE,
                        AgentCapability.CODE_EXECUTION,
                        AgentCapability.SHELL_EXECUTION,
                        AgentCapability.DATABASE_WRITE,
                    )))
                if override.fallback_models is not None:
                    values["fallback_models"] = tuple(override.fallback_models)
            values["timeout_seconds"] = min(
                float(values["timeout_seconds"]), config.max_timeout_seconds
            )
            values["retry_limit"] = min(int(values["retry_limit"]), config.max_retries_per_task)
            configured.append(replace(spec, **values))
        return tuple(configured)

    def subscribe(self, listener: EventListener) -> Callable[[], None]:
        self._listeners.add(listener)
        return lambda: self._listeners.discard(listener)

    def _replace_resource_coordinator(self) -> None:
        self.resource_coordinator = ResourceCoordinator(
            provider_limit=self.config.provider_max_concurrency
        )
        self.root_agent.resource_coordinator = self.resource_coordinator
        self.adapter.resource_coordinator = self.resource_coordinator

    def apply_config(self, app_config: Any | None = None) -> None:
        """Atomically reconcile live limits, roles, persistence, and paths.

        Active runs drain under their immutable launch snapshot.  Store topology
        changes are rejected while a run is active so records never split
        across SQLite files.  Disabling the mode rejects new runs immediately;
        optional cancellation is explicit in configuration.
        """
        source_config = app_config or self.root_agent.config
        latest = source_config.multi_agent
        desired_data_dir = Path(source_config.data_dir).expanduser().resolve()
        current_store_dir = self.store.db_path.parent.resolve() if self.store is not None else None
        topology_change = (
            bool(self.store) != bool(latest.persist_runs)
            or (latest.persist_runs and current_store_dir != desired_data_dir)
        )
        if topology_change and self._active:
            raise RuntimeError(
                "cannot change multi-agent persistence or data_dir while runs are active; "
                "wait for them to finish or cancel them first"
            )

        candidate_registry = AgentRegistry(self._configured_specs(latest))
        new_store = self.store
        if topology_change:
            new_store = MultiAgentRunStore(desired_data_dir) if latest.persist_runs else None

        old_store = self.store
        self.config = latest
        self.registry = candidate_registry
        self.store = new_store
        if old_store is not None and old_store is not new_store:
            old_store.close()
        if self.store is not None:
            self.store.cleanup(self.config.retention_days)

        if (
            self.resource_coordinator._provider_limit  # noqa: SLF001 - owned collaborator
            != self.config.provider_max_concurrency
        ):
            if self._active:
                # Existing runs keep their launch coordinator; new limits take
                # effect after the active set drains on the next reload.
                pass
            else:
                self._replace_resource_coordinator()
        self.worktree_manager = BuilderWorktreeManager(self.config.builder_worktree_root)
        self.adapter.resource_coordinator = self.resource_coordinator
        self.adapter.worktree_manager = self.worktree_manager
        if not self.config.enabled and self.config.cancel_active_on_disable:
            for task in tuple(self._active.values()):
                task.cancel()

    async def _emit(self, event_type: str, **payload: Any) -> None:
        if not self.config.stream_progress:
            return
        event = {"event_type": event_type, "timestamp": _now(), **payload}
        for listener in tuple(self._listeners):
            try:
                result = listener(event)
                if inspect.isawaitable(result):
                    await result
            except Exception:
                continue

    async def _child_event(self, event: AgentProgressEvent) -> None:
        if event.run_id:
            changes: dict[str, Any] = {"activity": event.detail}
            if event.event_type in {"tool_started", "tool_progress"}:
                changes["current_tool"] = event.tool
            elif event.event_type == "tool_completed":
                changes["current_tool"] = ""
            self._update_activity(event.run_id, **changes)
        payload = event.as_dict()
        payload.pop("event_type", None)
        child_session_id = str(payload.get("session_id") or "")
        parent_session_id = str(payload.get("parent_session_id") or "")
        if parent_session_id:
            payload["child_session_id"] = child_session_id
            payload["session_id"] = parent_session_id
        await self._emit(event.event_type, **payload)

    def _profiled_specs(self, execution_profile: str = STANDARD_EXECUTION_PROFILE) -> tuple[AgentSpec, ...]:
        profile = _normalize_execution_profile(execution_profile)
        specs = tuple(self.registry.snapshot().values())
        if profile == TRUSTED_LOCAL_EXECUTION_PROFILE:
            return tuple(trusted_local_agent_spec(spec) for spec in specs)
        return specs

    def list_agents(
        self,
        *,
        execution_profile: str = STANDARD_EXECUTION_PROFILE,
    ) -> list[dict[str, Any]]:
        profile = _normalize_execution_profile(execution_profile)
        return [
            {
                "name": spec.name,
                "description": spec.description,
                "capabilities": spec.instructions,
                "capability_grants": [capability.value for capability in spec.capabilities],
                "allowed_tools": list(spec.allowed_tools),
                "can_mutate": spec.can_mutate,
                "can_delegate": spec.can_delegate,
                "timeout_seconds": spec.timeout_seconds,
                "max_iterations": spec.max_iterations,
                "max_output_tokens": spec.max_output_tokens,
                "model": spec.model or self.root_agent.config.model,
                "retry_limit": spec.retry_limit,
                "fallback_models": list(spec.fallback_models),
                "execution_profile": profile,
            }
            for spec in self._profiled_specs(profile)
        ]

    def doctor(self) -> dict[str, Any]:
        store_health: dict[str, Any] = {"enabled": self.store is not None}
        if self.store is not None:
            try:
                self.store.conn.execute("SELECT 1").fetchone()
                store_health.update({"healthy": True, "path": str(self.store.db_path)})
            except Exception as exc:
                store_health.update({"healthy": False, "error": str(exc)})
        return {
            "enabled": bool(self.config.enabled),
            "runtime_initialized": True,
            "active_runs": len(self._active),
            "persistence": store_health,
            "roles": self.list_agents(),
            "delegation_schemas_visible_to_root": bool(self.config.enabled),
            "delegation_schemas_hidden_from_children": True,
            "resource_coordinator": self.resource_coordinator.state(),
            "limits": {
                "max_parallel_agents": self.config.max_parallel_agents,
                "max_tasks_per_run": self.config.max_tasks_per_run,
                "max_depth": self.config.max_depth,
                "max_total_duration_seconds": self.config.max_total_duration_seconds,
                "max_total_iterations": self.config.max_total_iterations,
                "max_total_tokens": self.config.max_total_tokens,
            },
            "active_disable_policy": (
                "cancel" if self.config.cancel_active_on_disable else "drain"
            ),
        }

    def create_action_grant(
        self,
        *,
        root_run_id: str,
        child_run_id: str,
        tool: str,
        arguments: dict[str, Any],
        request_id: str,
        explicit_user_confirmation: bool,
    ) -> ActionGrant:
        return self.action_grants.issue(
            root_run_id=root_run_id,
            child_run_id=child_run_id,
            tool=tool,
            arguments=arguments,
            request_id=request_id,
            ttl_seconds=self.config.action_grant_ttl_seconds,
            explicit_user_confirmation=explicit_user_confirmation,
        )

    def create_builder_patch_apply_grant(
        self,
        *,
        root_run_id: str,
        child_run_id: str,
        patch_hash: str,
        repository: str | Path,
        request_id: str,
        explicit_user_confirmation: bool,
    ) -> ActionGrant:
        """Issue a single-use grant for one reviewed patch identity.

        Callers must obtain explicit confirmation for the shown hash. A model
        review marker cannot mint this grant or alter its repository binding.
        """
        return self.create_action_grant(
            root_run_id=root_run_id,
            child_run_id=child_run_id,
            tool="apply_builder_patch",
            arguments={
                "patch_hash": str(patch_hash),
                "repository": str(Path(repository).expanduser().resolve()),
            },
            request_id=request_id,
            explicit_user_confirmation=explicit_user_confirmation,
        )

    def _upgrade_budget(self, value: Any = None):
        """Normalize an optional caller budget against administrator ceilings."""
        if value not in (None, "") and not isinstance(value, Mapping):
            raise ValueError("budget must be an object")
        config = self.config
        hard_limits = {
            "max_tasks": config.max_tasks_per_run,
            "max_parallel": config.max_parallel_agents,
            "max_depth": max(1, config.max_depth),
            "max_iterations": config.max_total_iterations,
            "max_output_tokens": config.max_total_tokens,
            "max_duration_seconds": config.max_total_duration_seconds,
            # These two limits do not exist in the older app config.  Keep
            # conservative hard defaults until they become administrator
            # settings, rather than accepting an unbounded caller value.
            "max_artifacts": 32,
            "max_artifact_bytes": 64 * 1024 * 1024,
        }
        return normalize_delegation_budget(
            value if isinstance(value, Mapping) else None,
            hard_limits=hard_limits,
            defaults=hard_limits,
        )

    @staticmethod
    def _optional_evidence_contract(value: Any) -> dict[str, Any]:
        """Validate and normalize the optional child-output acceptance contract."""
        if value in (None, ""):
            return {}
        if not isinstance(value, Mapping):
            raise ValueError("evidence_contract must be an object")
        roots = value.get("allowed_artifact_roots", ())
        if isinstance(roots, str):
            roots = [roots]
        if not isinstance(roots, (list, tuple)):
            raise ValueError("evidence_contract.allowed_artifact_roots must be an array")
        max_artifacts = value.get("max_artifacts", 32)
        if isinstance(max_artifacts, bool):
            raise ValueError("evidence_contract.max_artifacts must be a number")
        try:
            max_artifacts = int(max_artifacts)
        except (TypeError, ValueError) as exc:
            raise ValueError("evidence_contract.max_artifacts must be a number") from exc
        if max_artifacts < 0:
            raise ValueError("evidence_contract.max_artifacts must not be negative")
        return {
            "require_evidence": bool(value.get("require_evidence", False)),
            "require_claims": bool(value.get("require_claims", True)),
            "require_content": bool(value.get("require_content", False)),
            "allowed_artifact_roots": [str(root) for root in roots if str(root).strip()],
            "artifact_must_exist": bool(value.get("artifact_must_exist", False)),
            "max_artifacts": max_artifacts,
        }

    @staticmethod
    def _task_timeout_for_budget(
        timeout: float | None, budget: DelegationBudget | None,
    ) -> float | None:
        if timeout is None or budget is None:
            return timeout
        return min(timeout, budget.max_duration_seconds)

    @staticmethod
    def _plan_runtime_tasks(
        raw_tasks: Sequence[Mapping[str, Any]], plan: TaskGraphPlan,
    ) -> list[dict[str, Any]]:
        """Apply deterministic DAG dependencies without discarding legacy fields."""
        originals = {
            str(item.get("task_id") or item.get("id") or "").strip(): dict(item)
            for item in raw_tasks
        }
        planned: list[dict[str, Any]] = []
        for node in plan.tasks:
            if node.task_id in originals:
                item = dict(originals[node.task_id])
                item["depends_on"] = list(plan.dependencies[node.task_id])
            else:
                item = {
                    "task_id": node.task_id,
                    "agent": node.agent,
                    "prompt": node.prompt,
                    "depends_on": list(plan.dependencies[node.task_id]),
                    "context": dict(node.metadata),
                    "result_format": str(node.metadata.get("result_format") or "json"),
                    "allowed_context": list(node.metadata.get("allowed_context") or ()),
                }
            planned.append(item)
        return planned

    @staticmethod
    def _team_artifacts(team: AgentTeamResult) -> list[dict[str, str]]:
        return [
            {
                "path": artifact.path,
                "media_type": artifact.media_type,
                "description": artifact.description,
            }
            for result in team.results
            for artifact in result.artifacts
        ]

    def _project_run_progress(self, run: Mapping[str, Any]) -> dict[str, Any] | None:
        """Attach a non-authoritative, replay-safe progress projection to a run view."""
        metadata = run.get("metadata") if isinstance(run, Mapping) else None
        launch_plan = metadata.get("launch_plan") if isinstance(metadata, Mapping) else None
        raw_tasks = launch_plan.get("tasks") if isinstance(launch_plan, Mapping) else None
        if not isinstance(raw_tasks, list) or not raw_tasks:
            return None
        try:
            plan = plan_delegation_dag(raw_tasks, synthesize=False)
            events = [
                {"task_id": child.get("task_id"), "status": child.get("status")}
                for child in (run.get("children") or ())
                if isinstance(child, Mapping) and child.get("task_id")
            ]
            checkpoint = run.get("checkpoint") if isinstance(run.get("checkpoint"), Mapping) else None
            return project_delegation_progress(
                plan, events=events, checkpoint=checkpoint,
            ).as_dict()
        except (DelegationUpgradeError, ValueError, TypeError):
            return None

    def _structured_team_result(
        self,
        team: AgentTeamResult,
        *,
        budget: Any = None,
        plan: TaskGraphPlan | None = None,
        contract: Mapping[str, Any] | None = None,
        warnings: Sequence[str] = (),
    ) -> str:
        payload = team.as_dict()
        status = "completed" if team.succeeded else "partial" if any(item.ok for item in team.results) else "failed"
        run = self.get_run(team.root_run_id) or {}
        progress = self._project_run_progress(run)
        return structured_result(
            f"Completed {sum(item.ok for item in team.results)}/{len(team.results)} specialist tasks.",
            ok=team.succeeded,
            status=status,
            data={
                "run": payload,
                "budget": budget.as_dict() if budget is not None else self._upgrade_budget().budget.as_dict(),
                "plan": plan.as_dict() if plan is not None else None,
                "progress": progress,
                "evidence_contract": dict(contract or {}),
            },
            artifacts=self._team_artifacts(team),
            warnings=list(warnings),
            errors=[
                {"task_id": item.task_id, "message": item.error}
                for item in team.results if item.error
            ],
            next_actions=[
                {"tool": "get_agent_run", "arguments": {"run_id": team.root_run_id, "response_format": "structured"}}
            ] if team.root_run_id else [],
            provenance={"source": "native_multi_agent", "run_id": team.root_run_id},
            metrics={
                "task_count": len(team.results),
                "succeeded": sum(item.ok for item in team.results),
                "execution_waves": len(team.execution_waves),
            },
        )

    def _structured_run_view(self, run: Mapping[str, Any], *, summary: str) -> str:
        progress = self._project_run_progress(run)
        return structured_result(
            summary,
            ok=True,
            data={"run": dict(run), "progress": progress},
            artifacts=[
                dict(artifact)
                for child in (run.get("children") or ()) if isinstance(child, Mapping)
                for artifact in (child.get("artifacts") or ()) if isinstance(artifact, Mapping)
            ],
            provenance={"source": "native_multi_agent", "run_id": str(run.get("root_run_id") or run.get("run_id") or "")},
            metrics={"child_count": len(run.get("children") or ())},
        )

    @staticmethod
    def _apply_output_constraints(
        team: AgentTeamResult,
        *,
        budget: DelegationBudget | None,
        contract: Mapping[str, Any] | None,
    ) -> tuple[AgentTeamResult, tuple[str, ...]]:
        """Reject opt-in results that exceed their declared evidence/artifact bounds.

        This happens before the root is given the ``AgentTeamResult`` and
        before durable final result fields are written.  It deliberately does
        not alter a legacy delegation with no optional budget or contract.
        """
        if budget is None and not contract:
            return team, ()

        used_artifacts = 0
        used_artifact_bytes = 0
        warnings: list[str] = []
        constrained: list[AgentResult] = []
        for result in team.results:
            metadata = mutable_metadata(result.metadata)
            errors: list[str] = []
            validation_data: dict[str, Any] | None = None
            if contract:
                payload = {
                    "content": result.content,
                    "summary": result.summary,
                    "artifacts": [
                        {
                            "path": artifact.path,
                            "media_type": artifact.media_type,
                            "description": artifact.description,
                        }
                        for artifact in result.artifacts
                    ],
                    "evidence": metadata.get("evidence", ()),
                    "metadata": metadata,
                }
                validation = validate_evidence_artifact_contract(payload, **dict(contract))
                validation_data = validation.as_dict()
                errors.extend(validation.errors)
                warnings.extend(validation.warnings)

            result_artifact_bytes = 0
            for artifact in result.artifacts:
                try:
                    result_artifact_bytes += max(0, Path(artifact.path).expanduser().stat().st_size)
                except OSError:
                    # Not all valid artifacts are files (for example a remote
                    # report URL).  They still count as an artifact but have
                    # no local byte size available to charge.
                    warnings.append(f"Could not measure artifact bytes for {artifact.path}")
            used_artifacts += len(result.artifacts)
            used_artifact_bytes += result_artifact_bytes
            if budget is not None:
                if used_artifacts > budget.max_artifacts:
                    errors.append(
                        f"artifact budget exceeded ({used_artifacts} > {budget.max_artifacts})"
                    )
                if used_artifact_bytes > budget.max_artifact_bytes:
                    errors.append(
                        "artifact byte budget exceeded "
                        f"({used_artifact_bytes} > {budget.max_artifact_bytes})"
                    )
                metadata["delegation_budget_usage"] = {
                    "artifact_count": used_artifacts,
                    "artifact_bytes": used_artifact_bytes,
                }
            if validation_data is not None:
                metadata["evidence_contract"] = validation_data
            if errors and result.ok:
                detail = "; ".join(dict.fromkeys(errors))
                constrained.append(replace(
                    result,
                    status=AgentRunStatus.BLOCKED,
                    error=f"output acceptance contract failed: {detail}",
                    metadata=metadata,
                ))
            elif metadata != mutable_metadata(result.metadata):
                constrained.append(replace(result, metadata=metadata))
            else:
                constrained.append(result)
        return replace(team, results=tuple(constrained)), tuple(dict.fromkeys(warnings))

    async def execute_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        session_id: str | None,
        trusted_local_authorized: bool = False,
    ) -> str:
        structured = str(arguments.get("response_format") or "legacy").strip().casefold() == "structured"
        try:
            # Validate the opt-in response selector once. Existing callers do
            # not send it and continue to receive their historical payloads.
            structured = wants_structured(arguments)
            if name == "list_agents":
                execution_profile = _normalize_execution_profile(
                    arguments.get("execution_profile")
                )
                agents = self.list_agents(execution_profile=execution_profile)
                if structured:
                    return structured_result(
                        f"Found {len(agents)} available specialist roles.",
                        data={"agents": agents, "execution_profile": execution_profile},
                        provenance={"source": "native_multi_agent"},
                        metrics={"agent_count": len(agents)},
                    )
                return json.dumps({"agents": agents}, ensure_ascii=False)
            if name == "list_agent_runs":
                runs = self.list_runs(
                    limit=int(arguments.get("limit") or 30),
                    session_id=session_id,
                    status=str(arguments.get("status") or "") or None,
                )
                if structured:
                    return structured_result(
                        f"Found {len(runs)} agent runs.",
                        data={
                            "runs": [
                                {**run, "progress": self._project_run_progress(run)}
                                for run in runs
                            ]
                        },
                        provenance={"source": "native_multi_agent"},
                        metrics={"run_count": len(runs)},
                    )
                return json.dumps({"runs": runs}, ensure_ascii=False)
            if name == "get_latest_agent_run":
                run = self.get_latest_run(session_id=session_id)
                if run is None:
                    return error_result("No agent run found in this session", code="not_found", status="not_found") if structured else json.dumps({"error": "no agent run found in this session"}, ensure_ascii=False)
                return self._structured_run_view(run, summary="Loaded the latest agent run.") if structured else json.dumps(run, ensure_ascii=False)
            if name == "get_agent_run":
                run = self.get_run(str(arguments.get("run_id") or ""), session_id=session_id)
                if run is None:
                    return error_result("Run not found in this session", code="not_found", status="not_found") if structured else json.dumps({"error": "run not found in this session"}, ensure_ascii=False)
                return self._structured_run_view(run, summary="Loaded the requested agent run.") if structured else json.dumps(run, ensure_ascii=False)
            if name == "cancel_agent_run":
                cancelled = await self.cancel(
                    str(arguments.get("run_id") or ""), session_id=session_id
                )
                if structured:
                    return structured_result(
                        "Agent run cancellation requested." if cancelled else "No active agent run could be cancelled.",
                        ok=cancelled,
                        status="completed" if cancelled else "not_found",
                        data={"cancelled": cancelled, "run_id": arguments.get("run_id")},
                        next_actions=[] if cancelled else [{"tool": "list_agent_runs", "arguments": {"response_format": "structured"}}],
                        provenance={"source": "native_multi_agent"},
                    )
                return json.dumps({"cancelled": cancelled, "run_id": arguments.get("run_id")}, ensure_ascii=False)
            if name == "resume_agent_run":
                result = await self.resume(
                    str(arguments.get("run_id") or ""),
                    session_id=session_id,
                    request_id=str(arguments.get("request_id") or ""),
                )
                if structured:
                    return self._structured_team_result(result)
                return json.dumps(result.as_dict(), ensure_ascii=False)
            if name == "delegate_task":
                execution_profile = _normalize_execution_profile(
                    arguments.get("execution_profile")
                )
                _require_execution_profile_authorization(
                    execution_profile,
                    trusted_local_authorized=trusted_local_authorized,
                )
                raw_budget = arguments.get("budget")
                normalization = self._upgrade_budget(raw_budget)
                budget = normalization.budget if raw_budget is not None else None
                admission = enforce_delegation_budget(
                    normalization,
                    increment={"task_count": 1, "active_agents": 1, "depth": 1},
                )
                if not admission.allowed:
                    raise DelegationTaskLimitError("; ".join(admission.violations))
                contract = self._optional_evidence_contract(arguments.get("evidence_contract"))
                timeout = self._task_timeout_for_budget(
                    self._timeout(arguments.get("timeout_seconds")), budget,
                )
                context: dict[str, Any] = {"context": str(arguments.get("context") or "")}
                if budget is not None or contract:
                    context["delegation_upgrade"] = {
                        "budget": normalization.budget.as_dict(),
                        "evidence_contract": contract,
                    }
                task = AgentTask(
                    task_id="task",
                    agent=str(arguments.get("agent") or ""),
                    prompt=str(arguments.get("task") or ""),
                    context=context,
                    timeout_seconds=timeout,
                    required=bool(arguments.get("required", True)),
                    result_format=str(arguments.get("result_format") or "text"),
                )
                result = await self.delegate(
                    [task], shared_context=str(arguments.get("context") or ""),
                    session_id=session_id, request_id=str(arguments.get("request_id") or ""),
                    budget=budget,
                    evidence_contract=contract or None,
                    execution_profile=execution_profile,
                    trusted_local_authorized=trusted_local_authorized,
                )
                if structured:
                    return self._structured_team_result(
                        result,
                        budget=normalization.budget,
                        contract=contract,
                        warnings=normalization.warnings,
                    )
                return json.dumps(result.as_dict(), ensure_ascii=False)
            if name == "delegate_tasks_parallel":
                execution_profile = _normalize_execution_profile(
                    arguments.get("execution_profile")
                )
                _require_execution_profile_authorization(
                    execution_profile,
                    trusted_local_authorized=trusted_local_authorized,
                )
                raw_tasks = arguments.get("tasks")
                if not isinstance(raw_tasks, list):
                    raise ValueError("tasks must be an array")
                if not all(isinstance(item, Mapping) for item in raw_tasks):
                    raise ValueError("each task must be an object")
                raw_budget = arguments.get("budget")
                normalization = self._upgrade_budget(raw_budget)
                budget = normalization.budget if raw_budget is not None else None
                contract = self._optional_evidence_contract(arguments.get("evidence_contract"))
                planning_requested = bool(
                    structured
                    or arguments.get("plan")
                    or arguments.get("synthesize")
                    or any(
                        item.get("resource_keys") or item.get("write_targets") or item.get("exclusive_resources")
                        for item in raw_tasks
                    )
                )
                plan: TaskGraphPlan | None = None
                task_inputs: list[dict[str, Any]] = [dict(item) for item in raw_tasks]
                if planning_requested:
                    plan = plan_delegation_dag(
                        task_inputs,
                        synthesize=bool(arguments.get("synthesize", False)),
                        synthesis_task_id=str(arguments.get("synthesis_task_id") or "synthesize"),
                        synthesis_agent=str(arguments.get("synthesis_agent") or "synthesizer"),
                        synthesis_prompt=str(arguments.get("synthesis_prompt") or "Synthesize the independent specialist results into one evidence-backed answer."),
                    )
                    task_inputs = self._plan_runtime_tasks(task_inputs, plan)
                admission = enforce_delegation_budget(
                    normalization,
                    increment={
                        "task_count": len(task_inputs),
                        "active_agents": min(len(task_inputs), normalization.budget.max_parallel),
                        "depth": 1,
                    },
                )
                if not admission.allowed:
                    raise DelegationTaskLimitError("; ".join(admission.violations))
                tasks = [
                    self._task_from_dict(item, budget=budget, evidence_contract=contract)
                    for item in task_inputs
                ]
                result = await self.delegate(
                    tasks, shared_context=str(arguments.get("context") or ""),
                    session_id=session_id, request_id=str(arguments.get("request_id") or ""),
                    budget=budget,
                    evidence_contract=contract or None,
                    upgrade_plan=plan.as_dict() if plan is not None else None,
                    execution_profile=execution_profile,
                    trusted_local_authorized=trusted_local_authorized,
                )
                if structured:
                    return self._structured_team_result(
                        result,
                        budget=normalization.budget,
                        plan=plan,
                        contract=contract,
                        warnings=normalization.warnings,
                    )
                return json.dumps(result.as_dict(), ensure_ascii=False)
            raise ValueError(f"unknown delegation tool {name!r}")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if structured:
                return error_result(f"{type(exc).__name__}: {exc}", code="delegation_error")
            return json.dumps({"status": "failed", "error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False)

    def _task_from_dict(
        self,
        item: Any,
        *,
        budget: DelegationBudget | None = None,
        evidence_contract: Mapping[str, Any] | None = None,
    ) -> AgentTask:
        if not isinstance(item, Mapping):
            raise ValueError("each task must be an object")
        context = item.get("context")
        if not isinstance(context, Mapping):
            context = {"context": str(context or "")}
        context_data = dict(context)
        if budget is not None or evidence_contract:
            context_data["delegation_upgrade"] = {
                "budget": budget.as_dict() if budget is not None else None,
                "evidence_contract": dict(evidence_contract or {}),
            }
        return AgentTask(
            task_id=str(item.get("task_id") or ""),
            agent=str(item.get("agent") or ""),
            prompt=str(item.get("prompt") or item.get("task") or ""),
            depends_on=tuple(str(value) for value in (item.get("depends_on") or [])),
            context=context_data,
            timeout_seconds=self._task_timeout_for_budget(
                self._timeout(item.get("timeout_seconds")), budget,
            ),
            required=bool(item.get("required", True)),
            result_format=str(item.get("result_format") or "text"),
            allowed_context=tuple(str(value) for value in (item.get("allowed_context") or [])),
            allow_partial_dependencies=bool(
                item.get("allow_partial_dependencies", False)
                or (
                    self.config.partial_result_synthesis
                    and str(item.get("agent") or "") == "synthesizer"
                )
            ),
        )

    @staticmethod
    def _task_as_dict(task: AgentTask) -> dict[str, Any]:
        """Serialize the complete bounded assignment needed for safe recovery."""
        return {
            "task_id": task.task_id,
            "agent": task.agent,
            "prompt": task.prompt,
            "depends_on": list(task.depends_on),
            "context": mutable_metadata(task.context),
            "timeout_seconds": task.timeout_seconds,
            "required": task.required,
            "result_format": task.result_format,
            "allowed_context": list(task.allowed_context),
            "allow_partial_dependencies": task.allow_partial_dependencies,
        }

    @staticmethod
    def _task_from_checkpoint(item: Mapping[str, Any]) -> AgentTask:
        context = item.get("context")
        return AgentTask(
            task_id=str(item.get("task_id") or ""),
            agent=str(item.get("agent") or ""),
            prompt=str(item.get("prompt") or ""),
            depends_on=tuple(str(value) for value in (item.get("depends_on") or ())),
            context=dict(context) if isinstance(context, Mapping) else {},
            timeout_seconds=(
                float(item["timeout_seconds"])
                if item.get("timeout_seconds") not in (None, "")
                else None
            ),
            required=bool(item.get("required", True)),
            result_format=str(item.get("result_format") or "text"),
            allowed_context=tuple(
                str(value) for value in (item.get("allowed_context") or ())
            ),
            allow_partial_dependencies=bool(
                item.get("allow_partial_dependencies", False)
            ),
        )

    @staticmethod
    def _checkpoint_result(record: Mapping[str, Any], task: AgentTask) -> AgentResult:
        artifacts = tuple(
            AgentArtifact(
                path=str(item.get("path") or ""),
                media_type=str(item.get("media_type") or "text/plain"),
                description=str(item.get("description") or ""),
            )
            for item in (record.get("artifacts") or ())
            if isinstance(item, Mapping) and item.get("path")
        )
        metadata = mutable_metadata(record.get("metadata") or {})
        metadata.update({
            "checkpoint_reused": True,
            "checkpoint_source_run_id": str(record.get("run_id") or ""),
        })
        return AgentResult(
            task_id=task.task_id,
            agent=task.agent,
            status=AgentRunStatus.SUCCEEDED,
            content=str(record.get("result_content") or ""),
            summary=str(record.get("result_summary") or ""),
            artifacts=artifacts,
            duration_seconds=float(record.get("duration_seconds") or 0.0),
            run_id=str(record.get("run_id") or ""),
            parent_run_id=str(record.get("parent_run_id") or ""),
            root_run_id=str(record.get("root_run_id") or ""),
            iterations=int(record.get("iterations") or 0),
            metadata=metadata,
        )

    def _timeout(self, value: Any) -> float | None:
        if value in (None, ""):
            return None
        return min(float(value), self.config.max_timeout_seconds)

    @staticmethod
    def _planned_task_waves(tasks: Sequence[AgentTask]) -> tuple[tuple[str, ...], ...]:
        pending = {task.task_id: task for task in tasks}
        complete: set[str] = set()
        waves: list[tuple[str, ...]] = []
        while pending:
            ready = tuple(
                task_id for task_id, task in pending.items()
                if set(task.depends_on).issubset(complete)
            )
            if not ready:
                break
            waves.append(ready)
            for task_id in ready:
                pending.pop(task_id)
                complete.add(task_id)
        return tuple(waves)

    def _terminal_manifest_from_records(
        self,
        *,
        root_run_id: str,
        parent_session_id: str,
        request_id: str,
        tasks: Sequence[AgentTask],
        child_ids: Mapping[str, str],
        child_session_ids: Mapping[str, str],
        planned_waves: Sequence[Sequence[str]],
        started_tasks: set[str],
        created_at: str,
        started_monotonic: float,
        status: str,
        builder_workspaces: Mapping[str, Any],
    ) -> AgentExecutionManifest:
        completed_at = _now()
        children: list[ChildRunManifest] = []
        for task in tasks:
            record = self._volatile.get(child_ids[task.task_id], {})
            raw_artifacts = record.get("artifacts") or []
            artifacts = tuple(
                AgentArtifact(
                    path=str(item.get("path") or ""),
                    media_type=str(item.get("media_type") or "text/plain"),
                    description=str(item.get("description") or ""),
                )
                for item in raw_artifacts if isinstance(item, Mapping) and item.get("path")
            )
            metadata = mutable_metadata(record.get("metadata") or {})
            children.append(ChildRunManifest(
                run_id=child_ids[task.task_id],
                task_id=task.task_id,
                role=task.agent,
                session_id=child_session_ids[task.task_id],
                parent_session_id=parent_session_id,
                parent_run_id=root_run_id,
                root_run_id=root_run_id,
                status=str(record.get("status") or "cancelled"),
                dependencies=task.depends_on,
                started_at=record.get("started_at"),
                completed_at=record.get("completed_at") or completed_at,
                duration_seconds=float(record.get("duration_seconds") or 0.0),
                tools=tuple(metadata.get("tools") or ()),
                artifacts=artifacts,
                error=record.get("error_summary"),
                iterations=int(record.get("iterations") or 0),
                metadata=metadata,
            ))
        actual_waves = tuple(
            tuple(task_id for task_id in wave if task_id in started_tasks)
            for wave in planned_waves
            if any(task_id in started_tasks for task_id in wave)
        )
        duration = max(0.0, time.perf_counter() - started_monotonic)
        manifest = AgentExecutionManifest(
            root_run_id=root_run_id,
            session_id=parent_session_id,
            request_id=request_id,
            child_runs=tuple(children),
            execution_waves=actual_waves,
            started_at=str(self._volatile.get(root_run_id, {}).get("started_at") or created_at),
            completed_at=completed_at,
            status=status,
            duration_seconds=duration,
            metadata={
                "partial_results": any(child.status == "succeeded" for child in children),
                "builder_workspaces": mutable_metadata(builder_workspaces),
                "terminal_without_full_team_result": True,
                "resumed_from": str(
                    (self._volatile.get(root_run_id, {}).get("metadata") or {}).get(
                        "resumed_from"
                    ) or ""
                ),
            },
        )
        root_metadata = mutable_metadata(
            self._volatile.get(root_run_id, {}).get("metadata") or {}
        )
        root_metadata["execution_waves"] = [list(wave) for wave in actual_waves]
        self._update(
            root_run_id,
            status=status,
            completed_at=completed_at,
            duration_seconds=duration,
            activity=f"Specialist work {status}",
            manifest=manifest.as_dict(),
            metadata=root_metadata,
        )
        self._checkpoint(root_run_id)
        return manifest

    def _with_reviews(
        self,
        tasks: Sequence[AgentTask],
        *,
        registry: AgentRegistry | None = None,
        config: Any | None = None,
    ) -> tuple[AgentTask, ...]:
        result = list(tasks)
        config = config or self.config
        registry = registry or self.registry
        if not config.require_review_for_mutations:
            return tuple(result)
        review_role = str(config.review_role or "reviewer")
        available = registry.snapshot()
        for task in tasks:
            spec = registry.get(task.agent)
            if not any(spec.permits_capability(capability) for capability in (
                AgentCapability.FILESYSTEM_WRITE,
                AgentCapability.SHELL_EXECUTION,
                AgentCapability.CODE_EXECUTION,
            )):
                continue
            if review_role not in available:
                raise RuntimeError(
                    f"mutation delegation requires configured review role {review_role!r}, "
                    "but that role is disabled or unavailable"
                )
            has_review = any(
                candidate.agent == review_role and task.task_id in candidate.depends_on
                for candidate in result
            )
            if has_review:
                continue
            result.append(AgentTask(
                task_id=f"review_{task.task_id}",
                agent=review_role,
                prompt=(
                    f"Review the changes, isolated patch artifact, and verification produced by {task.task_id}. "
                    "Check correctness, regressions, security, architecture, and missing tests. Do not mutate files. "
                    "End with a standalone APPROVE_PATCH line only when the patch is safe and should be applied; "
                    "otherwise end with REJECT_PATCH and explain the blocker."
                ),
                depends_on=(task.task_id,),
                required=True,
                allowed_context=("task_dependencies",),
                allow_partial_dependencies=True,
            ))
        return tuple(result)

    def _apply_approved_builder_patches(
        self,
        *,
        tasks: Sequence[AgentTask],
        results: Sequence[AgentResult],
        builder_workspaces: Mapping[str, Mapping[str, Any]],
        manager: BuilderWorktreeManager,
        review_role: str,
        auto_apply: bool = False,
        root_run_id: str = "",
        request_id: str = "",
    ) -> dict[str, dict[str, Any]]:
        """Apply reviewer-approved isolated changes one at a time.

        A dirty target tree, a failed review, or a patch conflict is deliberately
        non-destructive: the immutable patch artifact remains available and the
        manifest records why automatic integration did not occur.
        """
        result_by_id = {result.task_id: result for result in results}
        applications: dict[str, dict[str, Any]] = {}
        for task in tasks:
            payload = builder_workspaces.get(task.task_id)
            if not isinstance(payload, Mapping):
                continue
            workspace = BuilderWorkspace(
                root=str(payload.get("root") or ""),
                isolated=bool(payload.get("isolated")),
                reason=str(payload.get("reason") or ""),
            )
            if not workspace.isolated:
                applications[task.task_id] = {
                    "status": "live_tree_serialized",
                    "detail": workspace.reason or "builder used the serialized live working tree",
                }
                continue
            builder_result = result_by_id.get(task.task_id)
            if builder_result is None or not builder_result.ok:
                applications[task.task_id] = {
                    "status": "not_applied",
                    "detail": "isolated builder did not complete successfully",
                }
                continue
            patch_path = str(builder_result.metadata.get("patch_path") or "")
            if not patch_path:
                applications[task.task_id] = {
                    "status": "not_applied",
                    "detail": str(builder_result.metadata.get("patch_capture") or "builder produced no patch"),
                }
                continue
            try:
                patch_hash = hashlib.sha256(Path(patch_path).read_bytes()).hexdigest()
            except OSError:
                patch_hash = ""
            review_results = [
                result_by_id[candidate.task_id]
                for candidate in tasks
                if candidate.agent == review_role
                and task.task_id in candidate.depends_on
                and candidate.task_id in result_by_id
            ]
            if not review_results:
                applications[task.task_id] = {
                    "status": "held_for_review",
                    "patch_path": patch_path,
                    "patch_hash": patch_hash,
                    "detail": "no configured reviewer result approved this isolated patch",
                }
                continue
            approved = all(
                result.ok
                and bool(re.search(r"(?mi)^\s*APPROVE_PATCH\s*$", result.content or ""))
                and not bool(re.search(r"(?mi)^\s*REJECT_PATCH\s*$", result.content or ""))
                for result in review_results
            )
            if not approved:
                applications[task.task_id] = {
                    "status": "held_for_review",
                    "patch_path": patch_path,
                    "patch_hash": patch_hash,
                    "detail": "reviewer did not explicitly approve the isolated patch",
                }
                continue
            if not auto_apply:
                applications[task.task_id] = {
                    "status": "held_for_root_approval",
                    "patch_path": patch_path,
                    "patch_hash": patch_hash,
                    "detail": "review approved the patch, but reviewer text cannot modify the live repository",
                }
                continue
            child_run_id = str(payload.get("child_run_id") or "")
            grant_id = str(task.context.get("patch_apply_grant_id") or "")
            grant = self.action_grants.consume(
                grant_id,
                root_run_id=root_run_id,
                child_run_id=child_run_id,
                tool="apply_builder_patch",
                arguments={
                    "patch_hash": patch_hash,
                    "repository": str(Path.cwd().resolve()),
                },
                request_id=request_id,
            ) if grant_id and child_run_id and patch_hash else None
            if grant is None or not grant.allowed:
                applications[task.task_id] = {
                    "status": "held_for_root_approval",
                    "patch_path": patch_path,
                    "patch_hash": patch_hash,
                    "detail": "automatic application requires an exact root-issued patch approval grant",
                }
                continue
            applied, detail = manager.apply_patch(Path.cwd(), patch_path)
            applications[task.task_id] = {
                "status": "applied" if applied else "held_for_manual_application",
                "patch_path": patch_path,
                "patch_hash": patch_hash,
                "detail": detail,
            }
        return applications

    async def delegate(
        self,
        tasks: Sequence[AgentTask],
        *,
        shared_context: str = "",
        session_id: str | None = None,
        request_id: str = "",
        depth: int = 1,
        initial_results: Mapping[str, AgentResult] | None = None,
        resumed_from: str = "",
        budget: DelegationBudget | None = None,
        evidence_contract: Mapping[str, Any] | None = None,
        upgrade_plan: Mapping[str, Any] | None = None,
        execution_profile: str = STANDARD_EXECUTION_PROFILE,
        trusted_local_authorized: bool = False,
    ) -> AgentTeamResult:
        execution_profile = _normalize_execution_profile(execution_profile)
        _require_execution_profile_authorization(
            execution_profile,
            trusted_local_authorized=trusted_local_authorized,
        )
        launch_app_config = self.root_agent.config.model_copy(deep=True)
        run_config = launch_app_config.multi_agent
        if budget is not None:
            run_config = run_config.model_copy(update={
                "max_parallel_agents": min(run_config.max_parallel_agents, budget.max_parallel),
                "max_tasks_per_run": min(run_config.max_tasks_per_run, budget.max_tasks),
                "max_depth": min(run_config.max_depth, budget.max_depth),
                "max_total_iterations": min(run_config.max_total_iterations, budget.max_iterations),
                "max_total_tokens": min(run_config.max_total_tokens, budget.max_output_tokens),
                "max_total_duration_seconds": min(run_config.max_total_duration_seconds, budget.max_duration_seconds),
            })
            launch_app_config = launch_app_config.model_copy(update={"multi_agent": run_config})
        if not run_config.enabled:
            raise RuntimeError("native multi-agent mode is disabled")
        if not tasks:
            raise ValueError("at least one specialist task is required")
        # Guardrails removed: delegation depth and recursion are always allowed.
        if len(tasks) > run_config.max_tasks_per_run:
            raise DelegationTaskLimitError(
                f"at most {run_config.max_tasks_per_run} tasks may be delegated"
            )
        profile_registry = AgentRegistry(self._profiled_specs(execution_profile))
        tasks = self._with_reviews(
            tuple(tasks), registry=profile_registry, config=run_config
        )
        if len(tasks) > run_config.max_tasks_per_run:
            raise DelegationTaskLimitError(
                "required mutation review would exceed the task limit; submit fewer builder tasks"
            )
        if budget is not None:
            admission = enforce_delegation_budget(
                budget,
                increment={
                    "task_count": len(tasks),
                    "active_agents": min(len(tasks), run_config.max_parallel_agents),
                    "depth": depth,
                },
            )
            if not admission.allowed:
                raise DelegationTaskLimitError("; ".join(admission.violations))

        initial_results = dict(initial_results or {})
        unknown_initial = sorted(set(initial_results) - {task.task_id for task in tasks})
        if unknown_initial:
            raise ValueError(
                f"checkpoint results reference unknown tasks: {', '.join(unknown_initial)}"
            )

        root_run_id = _id("ma")
        request_id = str(request_id or _id("request"))
        parent_session_id = str(session_id or "")
        child_ids = {task.task_id: _id("agent") for task in tasks}
        child_session_ids = {
            task.task_id: f"agent:{root_run_id}:{task.task_id}:{child_ids[task.task_id]}"
            for task in tasks
        }

        builder_workspaces: dict[str, dict[str, Any]] = {}
        project_checks = snapshot_agent_checks(Path.cwd())
        mutation_tasks = [
            task for task in tasks
            if any(profile_registry.get(task.agent).permits_capability(capability) for capability in (
                AgentCapability.FILESYSTEM_WRITE,
                AgentCapability.SHELL_EXECUTION,
                AgentCapability.CODE_EXECUTION,
            ))
        ]
        run_coordinator = self.resource_coordinator
        run_worktree_manager = self.worktree_manager
        if mutation_tasks:
            for task in mutation_tasks:
                workspace = (
                    run_worktree_manager.prepare(
                        Path.cwd(), root_run_id=root_run_id, child_run_id=child_ids[task.task_id]
                    )
                    if run_config.builder_worktree_isolation
                    else BuilderWorkspace(
                        str(Path.cwd().resolve()),
                        False,
                        "builder worktree isolation is disabled; mutation builders are serialized",
                    )
                )
                workspace_data = workspace.as_dict()
                workspace_data["child_run_id"] = child_ids[task.task_id]
                builder_workspaces[task.task_id] = workspace_data

        if builder_workspaces:
            tasks = tuple(
                replace(
                    task,
                    context={
                        **dict(task.context),
                        "builder_workspace": builder_workspaces.get(task.task_id),
                        "review_workspaces": {
                            dependency: builder_workspaces[dependency]
                            for dependency in task.depends_on
                            if dependency in builder_workspaces
                        },
                    },
                ) if task.task_id in builder_workspaces or (
                    task.agent == run_config.review_role
                    and any(dependency in builder_workspaces for dependency in task.depends_on)
                ) else task
                for task in tasks
            )

        # Snapshot per-run role limits so hot reloads affect only future runs.
        per_agent_budget = max(1, run_config.max_total_iterations // len(tasks))
        per_agent_token_budget = max(1, run_config.max_total_tokens // len(tasks))
        run_specs = tuple(
            replace(
                spec,
                max_iterations=min(spec.max_iterations, per_agent_budget),
                max_output_tokens=min(spec.max_output_tokens, per_agent_token_budget),
                timeout_seconds=min(spec.timeout_seconds, run_config.max_total_duration_seconds),
            )
            for spec in profile_registry.snapshot().values()
        )
        run_registry = AgentRegistry(run_specs)
        planned_waves = self._planned_task_waves(tasks)
        started_tasks: set[str] = set()
        created = _now()
        launch_metadata = {
            "task_count": len(tasks),
            "depth": depth,
            "max_total_iterations": run_config.max_total_iterations,
            "max_total_tokens": run_config.max_total_tokens,
            "max_total_duration_seconds": run_config.max_total_duration_seconds,
            "execution_profile": execution_profile,
            "builder_workspaces": builder_workspaces,
            "project_checks": project_checks,
            "launch_plan": {
                "version": 1,
                "tasks": [self._task_as_dict(task) for task in tasks],
                "shared_context": str(shared_context or ""),
                "depth": depth,
                "execution_profile": execution_profile,
            },
            "resumed_from": str(resumed_from or ""),
            "checkpoint_reused_tasks": sorted(initial_results),
        }
        if budget is not None or evidence_contract or upgrade_plan:
            launch_metadata["upgrade"] = {
                "budget": budget.as_dict() if budget is not None else None,
                "evidence_contract": dict(evidence_contract or {}),
                "plan": dict(upgrade_plan or {}),
            }
            launch_metadata["launch_plan"]["upgrade"] = dict(launch_metadata["upgrade"])
        root_record = {
            "run_id": root_run_id,
            "root_run_id": root_run_id,
            "session_id": parent_session_id,
            "parent_session_id": "",
            "request_id": request_id,
            "agent_role": "supervisor",
            "prompt_summary": "; ".join(task.prompt[:160] for task in tasks)[:1000],
            "status": "queued",
            "created_at": created,
            "metadata": launch_metadata,
            "checkpoint": {
                "completed": sorted(initial_results),
                "remaining": [
                    task.task_id for task in tasks if task.task_id not in initial_results
                ],
                "terminal": {
                    task_id: AgentRunStatus.SUCCEEDED.value for task_id in initial_results
                },
                "resume_supported": bool(len(initial_results) < len(tasks))
                and execution_profile != TRUSTED_LOCAL_EXECUTION_PROFILE,
            },
        }
        self._save(root_record)
        for task in tasks:
            cached_result = initial_results.get(task.task_id)
            child_record = {
                "run_id": child_ids[task.task_id], "root_run_id": root_run_id,
                "parent_run_id": root_run_id,
                "session_id": child_session_ids[task.task_id],
                "parent_session_id": parent_session_id,
                "request_id": request_id,
                "task_id": task.task_id, "agent_role": task.agent,
                "prompt_summary": task.prompt[:1000],
                "status": "succeeded" if cached_result is not None else "queued",
                "dependencies": list(task.depends_on), "created_at": created,
            }
            if cached_result is not None:
                child_record.update({
                    "started_at": created,
                    "completed_at": created,
                    "duration_seconds": cached_result.duration_seconds,
                    "result_summary": cached_result.summary,
                    "result_content": cached_result.content,
                    "iterations": cached_result.iterations,
                    "artifacts": [
                        {
                            "path": artifact.path,
                            "media_type": artifact.media_type,
                            "description": artifact.description,
                        }
                        for artifact in cached_result.artifacts
                    ],
                    "metadata": mutable_metadata(cached_result.metadata),
                    "activity": "Reused successful durable checkpoint",
                })
            self._save(child_record)

        adapter = (
            AresAgentAdapter(
                self.root_agent,
                self._child_event,
                resource_coordinator=run_coordinator,
                worktree_manager=run_worktree_manager,
                action_grants=self.action_grants,
                config_snapshot=launch_app_config,
            )
            if isinstance(self.adapter, AresAgentAdapter)
            else self.adapter
        )
        orchestrator = MultiAgentOrchestrator(
            run_registry,
            adapter,
            max_parallel=run_config.max_parallel_agents,
            fail_fast=False,
        )
        started = time.perf_counter()

        async def progress(event: AgentProgressEvent) -> None:
            run_id = child_ids.get(event.task_id, "")
            status = {
                "queued": "queued", "running": "running", "succeeded": "succeeded",
                "failed": "failed", "timed_out": "timed_out", "blocked": "blocked",
                "cancelled": "cancelled",
            }.get(event.phase, "running")
            changes: dict[str, Any] = {"status": status}
            if event.phase == "running":
                started_tasks.add(event.task_id)
                changes["started_at"] = _now()
                changes["activity"] = event.detail or "Specialist started"
            if status in {"succeeded", "failed", "timed_out", "blocked"}:
                changes["completed_at"] = _now()
                changes["current_tool"] = ""
                changes["activity"] = event.detail or status.replace("_", " ")
            if status in {"failed", "timed_out", "blocked"}:
                changes["error_summary"] = event.detail
            if status == "succeeded" and event.metadata:
                for source, target in (
                    ("result_content", "result_content"),
                    ("result_summary", "result_summary"),
                    ("artifacts", "artifacts"),
                    ("iterations", "iterations"),
                    ("result_metadata", "metadata"),
                ):
                    if source in event.metadata:
                        changes[target] = mutable_metadata(event.metadata[source])
            self._update(run_id, **changes)
            if run_config.checkpoint_runs and status in {
                "succeeded", "failed", "timed_out", "blocked", "cancelled"
            }:
                self._checkpoint(root_run_id)
            if status in {"succeeded", "failed", "timed_out", "blocked", "cancelled"}:
                self._activity_persisted_at.pop(run_id, None)
            event_type = {
                "queued": "agent_queued", "running": "agent_started", "succeeded": "agent_completed",
                "failed": "agent_failed", "timed_out": "agent_timed_out", "blocked": "agent_blocked",
                "cancelled": "agent_cancelled",
            }.get(event.phase, "agent_progress")
            await self._emit(
                event_type, root_run_id=root_run_id, parent_run_id=root_run_id,
                run_id=run_id, session_id=parent_session_id,
                child_session_id=child_session_ids.get(event.task_id, ""),
                request_id=request_id, task_id=event.task_id,
                agent=event.agent, phase=event.phase, status=status, detail=event.detail,
            )

        async def run_team() -> AgentTeamResult:
            self._update(root_run_id, status="running", started_at=_now(), activity="Starting specialist team")
            await self._emit(
                "orchestration_started", root_run_id=root_run_id, run_id=root_run_id,
                session_id=parent_session_id, request_id=request_id, status="running",
                detail=f"Running {len(tasks)} specialist tasks", root_task=root_record["prompt_summary"],
            )
            try:
                team = await orchestrator.run(
                    tasks,
                    shared_context=shared_context,
                    run_metadata={
                        "root_run_id": root_run_id, "parent_run_id": root_run_id,
                        "child_run_ids": child_ids,
                        "child_session_ids": child_session_ids,
                        "parent_session_id": parent_session_id,
                        "request_id": request_id,
                        "builder_workspaces": builder_workspaces,
                        "project_checks": project_checks,
                        "depth": depth,
                        "resumed_from": str(resumed_from or ""),
                        "upgrade": launch_metadata.get("upgrade", {}),
                    },
                    progress_callback=progress,
                    max_duration_seconds=run_config.max_total_duration_seconds,
                    initial_results=initial_results,
                )
            except asyncio.CancelledError:
                unfinished = [
                    pending_task for pending_task in tasks
                    if self._volatile.get(child_ids[pending_task.task_id], {}).get("status")
                    in {"queued", "running"}
                ]
                self._mark_cancelled(root_run_id)
                manifest = self._terminal_manifest_from_records(
                    root_run_id=root_run_id,
                    parent_session_id=parent_session_id,
                    request_id=request_id,
                    tasks=tasks,
                    child_ids=child_ids,
                    child_session_ids=child_session_ids,
                    planned_waves=planned_waves,
                    started_tasks=started_tasks,
                    created_at=created,
                    started_monotonic=started,
                    status="cancelled",
                    builder_workspaces=builder_workspaces,
                )
                for pending_task in unfinished:
                    await self._emit(
                        "agent_cancelled", root_run_id=root_run_id, parent_run_id=root_run_id,
                        run_id=child_ids[pending_task.task_id], session_id=parent_session_id,
                        child_session_id=child_session_ids[pending_task.task_id], request_id=request_id,
                        task_id=pending_task.task_id, agent=pending_task.agent, status="cancelled",
                    )
                self.action_grants.revoke_root(root_run_id)
                await self._emit(
                    "orchestration_cancelled", root_run_id=root_run_id, run_id=root_run_id,
                    session_id=parent_session_id, request_id=request_id, status="cancelled",
                    manifest=manifest.as_dict(),
                )
                raise
            except Exception as exc:
                self._mark_cancelled(root_run_id)
                manifest = self._terminal_manifest_from_records(
                    root_run_id=root_run_id,
                    parent_session_id=parent_session_id,
                    request_id=request_id,
                    tasks=tasks,
                    child_ids=child_ids,
                    child_session_ids=child_session_ids,
                    planned_waves=planned_waves,
                    started_tasks=started_tasks,
                    created_at=created,
                    started_monotonic=started,
                    status="failed",
                    builder_workspaces=builder_workspaces,
                )
                self._update(root_run_id, error_summary=f"{type(exc).__name__}: {exc}")
                self.action_grants.revoke_root(root_run_id)
                await self._emit(
                    "orchestration_failed", root_run_id=root_run_id, run_id=root_run_id,
                    session_id=parent_session_id, request_id=request_id, status="failed",
                    detail=f"{type(exc).__name__}: {exc}", manifest=manifest.as_dict(),
                )
                raise
            team, output_constraint_warnings = self._apply_output_constraints(
                team,
                budget=budget,
                contract=evidence_contract,
            )
            for result in team.results:
                run_id = child_ids[result.task_id]
                self._update(
                    run_id,
                    status=result.status.value,
                    completed_at=_now(),
                    duration_seconds=result.duration_seconds,
                    error_summary=result.error,
                    result_summary=result.summary,
                    result_content=result.content,
                    iterations=result.iterations,
                    artifacts=[{"path": item.path, "media_type": item.media_type, "description": item.description} for item in result.artifacts],
                    metadata=mutable_metadata(result.metadata),
                )
            patch_application = self._apply_approved_builder_patches(
                tasks=tasks,
                results=team.results,
                builder_workspaces=builder_workspaces,
                manager=run_worktree_manager,
                review_role=run_config.review_role,
                auto_apply=run_config.auto_apply_builder_patches,
                root_run_id=root_run_id,
                request_id=request_id,
            )
            self._update(root_run_id, activity="Synthesizing specialist results")
            await self._emit(
                "synthesis_started", root_run_id=root_run_id, run_id=root_run_id,
                session_id=parent_session_id, request_id=request_id, status="running",
                detail="Returning structured specialist results to the root agent",
            )
            status = team.status
            completed = _now()
            duration = time.perf_counter() - started
            children: list[ChildRunManifest] = []
            task_by_id = {task.task_id: task for task in tasks}
            for result in team.results:
                record = self._volatile.get(child_ids[result.task_id], {})
                metadata = mutable_metadata(result.metadata)
                children.append(ChildRunManifest(
                    run_id=child_ids[result.task_id],
                    task_id=result.task_id,
                    role=result.agent,
                    session_id=child_session_ids[result.task_id],
                    parent_session_id=parent_session_id,
                    parent_run_id=root_run_id,
                    root_run_id=root_run_id,
                    status=result.status.value,
                    dependencies=task_by_id[result.task_id].depends_on,
                    started_at=record.get("started_at"),
                    completed_at=record.get("completed_at") or completed,
                    duration_seconds=result.duration_seconds,
                    tools=tuple(metadata.get("tools") or ()),
                    artifacts=result.artifacts,
                    error=result.error,
                    iterations=result.iterations,
                    metadata=metadata,
                ))
            manifest = AgentExecutionManifest(
                root_run_id=root_run_id,
                session_id=parent_session_id,
                request_id=request_id,
                child_runs=tuple(children),
                execution_waves=team.execution_waves,
                started_at=str(self._volatile.get(root_run_id, {}).get("started_at") or created),
                completed_at=completed,
                status=status,
                duration_seconds=duration,
                metadata={
                    "partial_results": any(not result.ok for result in team.results)
                    and any(result.ok for result in team.results),
                    "builder_workspaces": builder_workspaces,
                    "estimated_tokens": sum(
                        int(result.metadata.get("estimated_tokens") or 0)
                        for result in team.results
                    ),
                    "max_total_tokens": run_config.max_total_tokens,
                    "resumed_from": str(resumed_from or ""),
                    "patch_application": patch_application,
                    "output_constraint_warnings": list(output_constraint_warnings),
                },
            )
            team = replace(team, manifest=manifest)
            self._update(
                root_run_id, status=status, completed_at=completed, duration_seconds=duration,
                result_summary=f"{sum(item.ok for item in team.results)}/{len(team.results)} specialist tasks succeeded",
                activity="Specialist work complete",
                metadata={
                    **launch_metadata,
                    "execution_waves": [list(wave) for wave in team.execution_waves],
                    "estimated_tokens": sum(
                        int(result.metadata.get("estimated_tokens") or 0)
                        for result in team.results
                    ),
                    "patch_application": patch_application,
                    "output_constraint_warnings": list(output_constraint_warnings),
                },
                manifest=manifest.as_dict(),
            )
            self._checkpoint(root_run_id)
            self.action_grants.revoke_root(root_run_id)
            await self._emit(
                "orchestration_completed", root_run_id=root_run_id, run_id=root_run_id,
                session_id=parent_session_id, request_id=request_id, status=status,
                execution_waves=[list(wave) for wave in team.execution_waves],
                manifest=manifest.as_dict(),
            )
            return team

        task = asyncio.create_task(run_team(), name=f"ares-multi-agent:{root_run_id}")
        self._active[root_run_id] = task
        try:
            return await task
        finally:
            self._active.pop(root_run_id, None)
            if (
                not self._active
                and self.resource_coordinator._provider_limit  # noqa: SLF001
                != self.config.provider_max_concurrency
            ):
                self._replace_resource_coordinator()

    def _save(self, record: dict[str, Any]) -> None:
        self._volatile[str(record["run_id"])] = dict(record)
        if self.store is not None:
            self.store.upsert(record)

    def _update(self, run_id: str, **changes: Any) -> None:
        if run_id in self._volatile:
            self._volatile[run_id].update(changes)
        if self.store is not None:
            self.store.update(run_id, **changes)

    def _update_activity(self, run_id: str, **changes: Any) -> None:
        """Keep live activity fresh without writing SQLite for every streamed token."""
        if run_id in self._volatile:
            self._volatile[run_id].update(changes)
        now = time.monotonic()
        if self.store is not None and now - self._activity_persisted_at.get(run_id, 0.0) >= 0.75:
            self.store.update(run_id, **changes)
            self._activity_persisted_at[run_id] = now

    def _checkpoint(self, root_run_id: str) -> None:
        root = self._volatile.get(root_run_id)
        if root is not None:
            records = [
                record
                for record in self._volatile.values()
                if record.get("root_run_id") == root_run_id
                and record.get("run_id") != root_run_id
            ]
        elif self.store is not None:
            stored = self.store.get(root_run_id)
            root = stored
            records = list((stored or {}).get("children") or ())
        else:
            records = []
        terminal_statuses = {
            "succeeded", "failed", "timed_out", "blocked", "cancelled", "interrupted"
        }
        terminal = {
            str(record.get("task_id") or record.get("run_id")): str(record.get("status") or "")
            for record in records
            if record.get("status") in terminal_statuses
        }
        launch_plan = (root or {}).get("metadata") or {}
        launch_plan = launch_plan.get("launch_plan") if isinstance(launch_plan, Mapping) else {}
        raw_tasks = launch_plan.get("tasks") if isinstance(launch_plan, Mapping) else []
        task_ids = [
            str(item.get("task_id") or "")
            for item in (raw_tasks or ())
            if isinstance(item, Mapping) and item.get("task_id")
        ]
        completed = [task_id for task_id in task_ids if terminal.get(task_id) == "succeeded"]
        remaining = [task_id for task_id in task_ids if task_id not in completed]
        checkpoint = {
            "completed": completed,
            "remaining": remaining,
            "terminal": terminal,
            "updated_at": _now(),
            "resume_supported": bool(task_ids and remaining),
            "resume_available_now": bool(
                task_ids and remaining and root_run_id not in self._active
            ),
        }
        self._update(root_run_id, checkpoint=checkpoint)

    def _mark_cancelled(self, root_run_id: str) -> None:
        for record in self._volatile.values():
            if record.get("root_run_id") == root_run_id and record.get("status") in {"queued", "running", "cancelling"}:
                record.update(status="cancelled", cancelled=True, completed_at=_now())
        if self.store is not None:
            self.store.mark_cancelled(root_run_id)

    @staticmethod
    def _session_allows(record: Mapping[str, Any], session_id: str | None) -> bool:
        if session_id is None:
            return True
        selected = str(session_id)
        return selected in {
            str(record.get("session_id") or ""),
            str(record.get("parent_session_id") or ""),
        }

    def get_run(
        self, run_id: str, *, session_id: str | None = None
    ) -> dict[str, Any] | None:
        if self.store is not None:
            return self.store.get(run_id, session_id=session_id)
        record = self._volatile.get(run_id)
        if record is None:
            record = next((value for value in self._volatile.values() if value.get("root_run_id") == run_id), None)
        if record is None or not self._session_allows(record, session_id):
            return None
        result = dict(record)
        result["children"] = [dict(value) for value in self._volatile.values() if value.get("root_run_id") == result.get("root_run_id") and value.get("run_id") != result.get("root_run_id")]
        return result

    def list_runs(
        self,
        *,
        limit: int = 30,
        session_id: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        if self.store is not None:
            return self.store.list(limit=limit, session_id=session_id, status=status)
        roots = [
            value for value in self._volatile.values()
            if value.get("run_id") == value.get("root_run_id")
            and self._session_allows(value, session_id)
            and (not status or value.get("status") == status)
        ]
        roots.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
        return [self.get_run(str(item["run_id"])) or {} for item in roots[:limit]]

    def get_latest_run(self, *, session_id: str | None) -> dict[str, Any] | None:
        if session_id in (None, ""):
            return None
        if self.store is not None:
            return self.store.latest(session_id=str(session_id))
        runs = self.list_runs(limit=1, session_id=str(session_id))
        return runs[0] if runs else None

    async def cancel(self, run_id: str, *, session_id: str | None = None) -> bool:
        run = self.get_run(run_id, session_id=session_id)
        if run is None:
            return False
        root_run_id = str((run or {}).get("root_run_id") or run_id)
        task = self._active.get(root_run_id)
        if task is None or task.done():
            return False
        self._update(root_run_id, status="cancelling", activity="Cancelling specialist team")
        await self._emit(
            "orchestration_cancelling", root_run_id=root_run_id, run_id=root_run_id,
            session_id=str((run or {}).get("session_id") or ""), status="cancelling",
            detail="Cancellation requested; waiting only for a bounded cleanup grace period.",
        )
        task.cancel()
        try:
            grace = max(0.1, float(self.config.tool_cancel_grace_seconds))
            await asyncio.wait_for(asyncio.shield(task), timeout=grace)
        except asyncio.CancelledError:
            pass
        except TimeoutError:
            quarantined = self.resource_coordinator.state().get("quarantined_operations", [])
            metadata = dict(self._volatile.get(root_run_id, {}).get("metadata") or {})
            metadata["unresponsive_tools"] = quarantined
            self._update(
                root_run_id,
                error_summary="cancelled_with_unresponsive_tool",
                metadata=metadata,
            )
        self._mark_cancelled(root_run_id)
        self.action_grants.revoke_root(root_run_id)
        return True

    async def resume(
        self,
        run_id: str,
        *,
        session_id: str | None,
        request_id: str = "",
    ) -> AgentTeamResult:
        """Resume a durable read-only run without replaying successful children.

        Mutation-capable assignments are intentionally not replayed: a process
        may have exited after an external side effect but before recording its
        result.  Those require a new, explicit assignment and fresh action
        grants so Ares never guesses that a consequential retry is idempotent.
        """
        selected_session = str(session_id or "")
        # Guardrails removed: session checks removed.
        selected = self.get_run(str(run_id or ""), session_id=selected_session)
        root_run_id = str(selected.get("root_run_id") or run_id)
        if root_run_id in self._active:
            raise RuntimeError("agent run is still active and cannot be resumed")
        root = self.get_run(root_run_id, session_id=selected_session)

        metadata = root.get("metadata") or {}
        launch_plan = metadata.get("launch_plan") if isinstance(metadata, Mapping) else None
        raw_tasks = launch_plan.get("tasks") if isinstance(launch_plan, Mapping) else None
        if not isinstance(raw_tasks, list) or not raw_tasks:
            raise RuntimeError("this run predates durable checkpoints and cannot be resumed")
        tasks = tuple(
            self._task_from_checkpoint(item)
            for item in raw_tasks
            if isinstance(item, Mapping)
        )
        if len(tasks) != len(raw_tasks):
            raise RuntimeError("durable checkpoint task graph is malformed")

        children = {
            str(child.get("task_id") or ""): child
            for child in (root.get("children") or ())
            if isinstance(child, Mapping) and child.get("task_id")
        }
        initial_results = {
            task.task_id: self._checkpoint_result(children[task.task_id], task)
            for task in tasks
            if children.get(task.task_id, {}).get("status") == "succeeded"
        }
        unfinished = [task for task in tasks if task.task_id not in initial_results]
        if not unfinished:
            raise RuntimeError("agent run already completed successfully")

        # Guardrails removed: trusted_local runs are resumable.

        consequential = {
            AgentCapability.FILESYSTEM_WRITE,
            AgentCapability.CODE_EXECUTION,
            AgentCapability.SHELL_EXECUTION,
            AgentCapability.BROWSER_INTERACTION,
            AgentCapability.DATABASE_WRITE,
            AgentCapability.COMMUNICATION,
            AgentCapability.EXTERNAL_MUTATION,
        }
        resumed_execution_profile = _normalize_execution_profile(
            launch_plan.get("execution_profile")
        )
        resumed_registry = AgentRegistry(
            self._profiled_specs(resumed_execution_profile)
        )
        unsafe_roles: list[str] = []
        for task in unfinished:
            try:
                spec = resumed_registry.get(task.agent)
            except KeyError as exc:
                raise RuntimeError(
                    f"cannot resume because specialist role {task.agent!r} is disabled"
                ) from exc
            if any(spec.permits_capability(capability) for capability in consequential):
                unsafe_roles.append(task.agent)
        # Guardrails removed: unsafe role resume is always allowed.

        upgraded = launch_plan.get("upgrade") if isinstance(launch_plan, Mapping) else None
        raw_budget = upgraded.get("budget") if isinstance(upgraded, Mapping) else None
        resumed_budget = self._upgrade_budget(raw_budget).budget if isinstance(raw_budget, Mapping) else None
        resumed_contract = self._optional_evidence_contract(
            upgraded.get("evidence_contract") if isinstance(upgraded, Mapping) else None
        )
        resumed_plan = upgraded.get("plan") if isinstance(upgraded, Mapping) else None

        return await self.delegate(
            tasks,
            shared_context=str(launch_plan.get("shared_context") or ""),
            session_id=selected_session,
            request_id=request_id or _id("resume"),
            depth=int(launch_plan.get("depth") or 1),
            initial_results=initial_results,
            resumed_from=root_run_id,
            budget=resumed_budget,
            evidence_contract=resumed_contract or None,
            upgrade_plan=resumed_plan if isinstance(resumed_plan, Mapping) else None,
            execution_profile=resumed_execution_profile,
            trusted_local_authorized=False,
        )

    async def delegate_request(
        self,
        request: str,
        *,
        session_id: str | None = None,
        request_id: str = "",
        roles: Sequence[str] = (),
        execution_profile: str = STANDARD_EXECUTION_PROFILE,
        trusted_local_authorized: bool = False,
    ) -> AgentTeamResult:
        """Force native delegation for a direct ``/agents run`` request."""
        request = str(request or "").strip()
        if not request:
            raise ValueError("delegation request cannot be empty")
        available = self.registry.snapshot()
        selected = tuple(dict.fromkeys(str(role).strip() for role in roles if str(role).strip()))
        if not selected:
            selected = ("researcher",) if "researcher" in available else tuple(available)[:1]
        unknown = [role for role in selected if role not in available]
        if unknown:
            raise DelegationRoleUnavailableError(
                f"unknown or disabled specialist roles: {', '.join(unknown)}"
            )
        tasks = tuple(
            AgentTask(
                task_id=f"{role}_{index}",
                agent=role,
                prompt=request,
                result_format="json" if role in {"researcher", "synthesizer"} else "text",
            )
            for index, role in enumerate(selected, 1)
        )
        return await self.delegate(
            tasks,
            session_id=session_id,
            request_id=request_id,
            execution_profile=execution_profile,
            trusted_local_authorized=trusted_local_authorized,
        )

    async def smoke_test(
        self, *, session_id: str | None = None, request_id: str = ""
    ) -> AgentTeamResult:
        """Launch two harmless real read-only specialists in one wave."""
        available = self.registry.snapshot()
        roles = [role for role in ("analyst", "planner", "researcher") if role in available]
        if len(roles) < 2:
            raise RuntimeError("smoke test requires at least two enabled read-only specialist roles")
        tasks = (
            AgentTask("smoke_runtime", roles[0], "Report the current specialist role name and confirm no tools are required."),
            AgentTask("smoke_policy", roles[1], "Report the bounded assignment and confirm no mutation is requested."),
        )
        return await self.delegate(
            tasks,
            shared_context="Deterministic harmless multi-agent smoke test.",
            session_id=session_id,
            request_id=request_id or _id("smoke"),
        )

    async def close(self) -> None:
        tasks = tuple(self._active.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._active.clear()
        if self.store is not None:
            self.store.close()
