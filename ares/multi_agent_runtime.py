"""Production supervisor that exposes Ares' native specialists to root agents."""

from __future__ import annotations

import asyncio
import inspect
import json
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from ares.multi_agent import (
    AgentProgressEvent,
    AgentRegistry,
    AgentResult,
    AgentRunStatus,
    AgentSpec,
    AgentTask,
    AgentTeamResult,
    MultiAgentOrchestrator,
    default_agent_specs,
)
from ares.multi_agent_adapter import AresAgentAdapter
from ares.multi_agent_store import MultiAgentRunStore


EventListener = Callable[[dict[str, Any]], Awaitable[None] | None]


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


class MultiAgentRuntime:
    """One root-owned, cancellation-aware supervisor with no global run state."""

    def __init__(self, root_agent: Any) -> None:
        self.root_agent = root_agent
        self.config = root_agent.config.multi_agent
        self.registry = AgentRegistry(self._configured_specs())
        self.store = MultiAgentRunStore(root_agent.config.data_dir) if self.config.persist_runs else None
        self._listeners: set[EventListener] = set()
        self._active: dict[str, asyncio.Task[AgentTeamResult]] = {}
        self._volatile: dict[str, dict[str, Any]] = {}
        self.adapter = AresAgentAdapter(root_agent, self._child_event)
        if self.store is not None:
            self.store.cleanup(self.config.retention_days)

    def _configured_specs(self) -> tuple[AgentSpec, ...]:
        configured: list[AgentSpec] = []
        for spec in default_agent_specs():
            override = self.config.role_overrides.get(spec.name)
            if override is not None and not override.enabled:
                continue
            values: dict[str, Any] = {
                "timeout_seconds": min(self.config.default_timeout_seconds, self.config.max_timeout_seconds),
            }
            model_override = self.config.model_overrides_by_role.get(spec.name)
            if model_override:
                values["model"] = model_override
            if override is not None:
                for key in ("model", "max_iterations", "timeout_seconds", "can_mutate", "can_delegate"):
                    value = getattr(override, key)
                    if value is not None:
                        values[key] = value
                if override.allowed_tools is not None:
                    values["allowed_tools"] = tuple(override.allowed_tools)
            values["timeout_seconds"] = min(
                float(values["timeout_seconds"]), self.config.max_timeout_seconds
            )
            configured.append(replace(spec, **values))
        return tuple(configured)

    def subscribe(self, listener: EventListener) -> Callable[[], None]:
        self._listeners.add(listener)
        return lambda: self._listeners.discard(listener)

    def apply_config(self) -> None:
        """Refresh limits and role overrides after Ares hot-reloads config."""
        self.config = self.root_agent.config.multi_agent
        self.registry = AgentRegistry(self._configured_specs())
        if self.store is not None:
            self.store.cleanup(self.config.retention_days)

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
        payload = event.as_dict()
        payload.pop("event_type", None)
        await self._emit(event.event_type, **payload)

    def list_agents(self) -> list[dict[str, Any]]:
        return [
            {
                "name": spec.name,
                "description": spec.description,
                "capabilities": spec.instructions,
                "allowed_tools": list(spec.allowed_tools),
                "can_mutate": spec.can_mutate,
                "can_delegate": spec.can_delegate,
                "timeout_seconds": spec.timeout_seconds,
                "max_iterations": spec.max_iterations,
                "model": spec.model or self.root_agent.config.model,
            }
            for spec in self.registry.snapshot().values()
        ]

    async def execute_tool(self, name: str, arguments: dict[str, Any], *, session_id: str | None) -> str:
        try:
            if name == "list_agents":
                return json.dumps({"agents": self.list_agents()}, ensure_ascii=False)
            if name == "get_agent_run":
                return json.dumps(self.get_run(str(arguments.get("run_id") or "")) or {"error": "run not found"}, ensure_ascii=False)
            if name == "cancel_agent_run":
                cancelled = await self.cancel(str(arguments.get("run_id") or ""))
                return json.dumps({"cancelled": cancelled, "run_id": arguments.get("run_id")}, ensure_ascii=False)
            if name == "delegate_task":
                task = AgentTask(
                    task_id="task",
                    agent=str(arguments.get("agent") or ""),
                    prompt=str(arguments.get("task") or ""),
                    context={"context": str(arguments.get("context") or "")},
                    timeout_seconds=self._timeout(arguments.get("timeout_seconds")),
                    required=bool(arguments.get("required", True)),
                    result_format=str(arguments.get("result_format") or "text"),
                )
                result = await self.delegate([task], shared_context=str(arguments.get("context") or ""), session_id=session_id)
                return json.dumps(result.as_dict(), ensure_ascii=False)
            if name == "delegate_tasks_parallel":
                raw_tasks = arguments.get("tasks")
                if not isinstance(raw_tasks, list):
                    raise ValueError("tasks must be an array")
                tasks = [self._task_from_dict(item) for item in raw_tasks]
                result = await self.delegate(tasks, shared_context=str(arguments.get("context") or ""), session_id=session_id)
                return json.dumps(result.as_dict(), ensure_ascii=False)
            raise ValueError(f"unknown delegation tool {name!r}")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return json.dumps({"status": "failed", "error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False)

    def _task_from_dict(self, item: Any) -> AgentTask:
        if not isinstance(item, Mapping):
            raise ValueError("each task must be an object")
        context = item.get("context")
        if not isinstance(context, Mapping):
            context = {"context": str(context or "")}
        return AgentTask(
            task_id=str(item.get("task_id") or ""),
            agent=str(item.get("agent") or ""),
            prompt=str(item.get("prompt") or item.get("task") or ""),
            depends_on=tuple(str(value) for value in (item.get("depends_on") or [])),
            context=dict(context),
            timeout_seconds=self._timeout(item.get("timeout_seconds")),
            required=bool(item.get("required", True)),
            result_format=str(item.get("result_format") or "text"),
        )

    def _timeout(self, value: Any) -> float | None:
        if value in (None, ""):
            return None
        return min(float(value), self.config.max_timeout_seconds)

    def _with_reviews(self, tasks: Sequence[AgentTask]) -> tuple[AgentTask, ...]:
        result = list(tasks)
        if not self.config.require_review_for_mutations:
            return tuple(result)
        for task in tasks:
            spec = self.registry.get(task.agent)
            if not spec.can_mutate:
                continue
            has_review = any(candidate.agent == "reviewer" and task.task_id in candidate.depends_on for candidate in result)
            if has_review:
                continue
            result.append(AgentTask(
                task_id=f"review_{task.task_id}",
                agent="reviewer",
                prompt=f"Review the changes and verification produced by {task.task_id}. Check correctness, regressions, security, architecture, and missing tests. Do not mutate files.",
                depends_on=(task.task_id,),
                required=True,
            ))
        return tuple(result)

    async def delegate(
        self,
        tasks: Sequence[AgentTask],
        *,
        shared_context: str = "",
        session_id: str | None = None,
        depth: int = 1,
    ) -> AgentTeamResult:
        if not self.config.enabled:
            raise RuntimeError("native multi-agent mode is disabled")
        if depth > self.config.max_depth:
            raise PermissionError(f"delegation depth {depth} exceeds configured maximum {self.config.max_depth}")
        if depth > 1 and not self.config.allow_recursive_delegation:
            raise PermissionError("recursive delegation is disabled")
        if len(tasks) > self.config.max_tasks_per_run:
            raise ValueError(f"at most {self.config.max_tasks_per_run} tasks may be delegated")
        tasks = self._with_reviews(tuple(tasks))
        if len(tasks) > self.config.max_tasks_per_run:
            raise ValueError("required mutation review would exceed the task limit; submit fewer builder tasks")

        root_run_id = _id("ma")
        child_ids = {task.task_id: _id("agent") for task in tasks}
        created = _now()
        root_record = {
            "run_id": root_run_id,
            "root_run_id": root_run_id,
            "session_id": str(session_id or ""),
            "agent_role": "supervisor",
            "prompt_summary": "; ".join(task.prompt[:160] for task in tasks)[:1000],
            "status": "queued",
            "created_at": created,
            "metadata": {"task_count": len(tasks), "depth": depth},
        }
        self._save(root_record)
        for task in tasks:
            self._save({
                "run_id": child_ids[task.task_id], "root_run_id": root_run_id,
                "parent_run_id": root_run_id, "session_id": str(session_id or ""),
                "task_id": task.task_id, "agent_role": task.agent,
                "prompt_summary": task.prompt[:1000], "status": "queued",
                "dependencies": list(task.depends_on), "created_at": created,
            })

        adapter = self.adapter
        orchestrator = MultiAgentOrchestrator(
            self.registry,
            adapter,
            max_parallel=self.config.max_parallel_agents,
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
                changes["started_at"] = _now()
            if status in {"succeeded", "failed", "timed_out", "blocked"}:
                changes["completed_at"] = _now()
            if status in {"failed", "timed_out", "blocked"}:
                changes["error_summary"] = event.detail
            self._update(run_id, **changes)
            event_type = {
                "queued": "agent_queued", "running": "agent_started", "succeeded": "agent_completed",
                "failed": "agent_failed", "timed_out": "agent_timed_out", "blocked": "agent_blocked",
                "cancelled": "agent_cancelled",
            }.get(event.phase, "agent_progress")
            await self._emit(
                event_type, root_run_id=root_run_id, parent_run_id=root_run_id,
                run_id=run_id, session_id=str(session_id or ""), task_id=event.task_id,
                agent=event.agent, phase=event.phase, status=status, detail=event.detail,
            )

        async def run_team() -> AgentTeamResult:
            self._update(root_run_id, status="running", started_at=_now())
            await self._emit(
                "orchestration_started", root_run_id=root_run_id, run_id=root_run_id,
                session_id=str(session_id or ""), status="running",
                detail=f"Running {len(tasks)} specialist tasks", root_task=root_record["prompt_summary"],
            )
            try:
                team = await orchestrator.run(
                    tasks,
                    shared_context=shared_context,
                    run_metadata={
                        "root_run_id": root_run_id, "parent_run_id": root_run_id,
                        "child_run_ids": child_ids, "session_id": str(session_id or ""), "depth": depth,
                    },
                    progress_callback=progress,
                )
            except asyncio.CancelledError:
                self._mark_cancelled(root_run_id)
                for pending_task in tasks:
                    await self._emit(
                        "agent_cancelled", root_run_id=root_run_id, parent_run_id=root_run_id,
                        run_id=child_ids[pending_task.task_id], session_id=str(session_id or ""),
                        task_id=pending_task.task_id, agent=pending_task.agent, status="cancelled",
                    )
                await self._emit("orchestration_cancelled", root_run_id=root_run_id, run_id=root_run_id, status="cancelled")
                raise
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
                    metadata=dict(result.metadata),
                )
            status = team.status
            self._update(
                root_run_id, status=status, completed_at=_now(), duration_seconds=time.perf_counter() - started,
                result_summary=f"{sum(item.ok for item in team.results)}/{len(team.results)} specialist tasks succeeded",
                metadata={"execution_waves": [list(wave) for wave in team.execution_waves]},
            )
            await self._emit("synthesis_started", root_run_id=root_run_id, run_id=root_run_id, status="running", detail="Returning structured specialist results to the root agent")
            await self._emit("orchestration_completed", root_run_id=root_run_id, run_id=root_run_id, status=status, execution_waves=[list(wave) for wave in team.execution_waves])
            return team

        task = asyncio.create_task(run_team(), name=f"ares-multi-agent:{root_run_id}")
        self._active[root_run_id] = task
        try:
            return await task
        finally:
            self._active.pop(root_run_id, None)

    def _save(self, record: dict[str, Any]) -> None:
        self._volatile[str(record["run_id"])] = dict(record)
        if self.store is not None:
            self.store.upsert(record)

    def _update(self, run_id: str, **changes: Any) -> None:
        if run_id in self._volatile:
            self._volatile[run_id].update(changes)
        if self.store is not None:
            self.store.update(run_id, **changes)

    def _mark_cancelled(self, root_run_id: str) -> None:
        for record in self._volatile.values():
            if record.get("root_run_id") == root_run_id and record.get("status") in {"queued", "running"}:
                record.update(status="cancelled", cancelled=True, completed_at=_now())
        if self.store is not None:
            self.store.mark_cancelled(root_run_id)

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        if self.store is not None:
            return self.store.get(run_id)
        record = self._volatile.get(run_id)
        if record is None:
            record = next((value for value in self._volatile.values() if value.get("root_run_id") == run_id), None)
        if record is None:
            return None
        result = dict(record)
        result["children"] = [dict(value) for value in self._volatile.values() if value.get("root_run_id") == result.get("root_run_id") and value.get("run_id") != result.get("root_run_id")]
        return result

    def list_runs(self, *, limit: int = 30, session_id: str | None = None) -> list[dict[str, Any]]:
        if self.store is not None:
            return self.store.list(limit=limit, session_id=session_id)
        roots = [value for value in self._volatile.values() if value.get("run_id") == value.get("root_run_id")]
        roots.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
        return [self.get_run(str(item["run_id"])) or {} for item in roots[:limit]]

    async def cancel(self, run_id: str) -> bool:
        run = self.get_run(run_id)
        root_run_id = str((run or {}).get("root_run_id") or run_id)
        task = self._active.get(root_run_id)
        if task is None or task.done():
            return False
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        self._mark_cancelled(root_run_id)
        return True

    async def close(self) -> None:
        tasks = tuple(self._active.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._active.clear()
        if self.store is not None:
            self.store.close()
