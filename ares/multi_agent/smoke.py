"""Deterministic production-path smoke coverage for native multi-agent mode.

Every acceptance scenario enters through a real :class:`ares.agent.Agent` and,
when agents are expected to run, a real
:class:`ares.multi_agent.runtime.MultiAgentRuntime`.  Deterministic fakes are
used only at the model/specialist-tool boundary, so the suite stays local and
free while still exercising routing, turn authorization, orchestration,
persistence, manifests, session scoping, and resource coordination.

Run it with::

    python -m ares.multi_agent_smoke
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, AsyncIterator, Mapping, Sequence

from ares.agent import Agent
from ares.memory import MemoryStore
from ares.models import AppConfig, MultiAgentConfig
from ares.multi_agent import AgentExecutionContext, AgentOutput, AgentSpec, AgentTask
from ares.multi_agent.research import ResearchClaim, parse_research_claims
from ares.multi_agent.runtime import MultiAgentRuntime
from ares.integrations.turn_policy import build_turn_execution_context


_RESEARCH_SESSION_ID = "smoke-session-scenario-b"
_RESEARCH_REQUEST_ID = "smoke-request-scenario-b"


@dataclass(frozen=True, slots=True)
class SmokeScenarioResult:
    """One stable smoke result plus machine-checkable production evidence."""

    scenario: str
    detail: str
    evidence: Mapping[str, Any] = field(default_factory=dict, repr=False)


def _check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


class _ZeroEmbeddingProvider:
    """Minimal deterministic provider used only by the empty smoke database."""

    def embed_bytes(self, _text: str) -> bytes:
        return b"\0" * (384 * 4)


class _ScriptedRootLLM:
    """Streaming root-model boundary with observable tool visibility."""

    def __init__(
        self,
        config: AppConfig,
        turns: Sequence[Sequence[Mapping[str, Any]]] = (),
        *,
        forbid_calls: bool = False,
    ) -> None:
        self.config = config
        self._turns = [tuple(dict(event) for event in turn) for turn in turns]
        self.forbid_calls = forbid_calls
        self.calls: list[dict[str, Any]] = []
        self.closed = False

    async def chat_stream(
        self, messages: list[dict], tools: list[dict] | None = None
    ) -> AsyncIterator[dict[str, Any]]:
        if self.forbid_calls:
            raise AssertionError("this scenario must terminate before calling the root model")
        index = len(self.calls)
        if index >= len(self._turns):
            raise AssertionError(f"unexpected root-model call {index + 1}")
        self.calls.append({
            "messages": [dict(message) for message in messages],
            "tools": [item["function"]["name"] for item in (tools or [])],
        })
        for event in self._turns[index]:
            yield dict(event)

    async def close(self) -> None:
        self.closed = True


@dataclass(slots=True)
class _AgentFixture:
    agent: Agent
    memory: MemoryStore
    llm: _ScriptedRootLLM

    async def close(self) -> None:
        await self.agent.close()
        self.memory.close()


def _make_agent(
    root: Path,
    name: str,
    *,
    session_id: str,
    enabled: bool,
    turns: Sequence[Sequence[Mapping[str, Any]]] = (),
    forbid_llm_calls: bool = False,
    max_parallel_agents: int = 3,
) -> _AgentFixture:
    data_dir = root / name / "data"
    config = AppConfig(
        data_dir=str(data_dir),
        project_context_enabled=False,
        skills_enabled=False,
        skill_auto_suggest=False,
        skill_dirs=[],
        mcp_servers=[],
        enable_desktop_notifications=False,
        multi_agent=MultiAgentConfig(
            enabled=enabled,
            max_parallel_agents=max_parallel_agents,
            max_retries_per_task=0,
            retry_backoff_seconds=0.0,
            persist_runs=True,
            builder_worktree_root=str(root / name / "worktrees"),
        ),
    )
    memory = MemoryStore(
        db_path=root / name / "memory.db",
        embedding_provider=_ZeroEmbeddingProvider(),
    )
    llm = _ScriptedRootLLM(config, turns, forbid_calls=forbid_llm_calls)
    agent = Agent(
        memory_store=memory,
        config=config,
        llm_client=llm,  # type: ignore[arg-type]
        session_id=session_id,
    )
    _check(isinstance(agent, Agent), "smoke fixture did not construct the production Agent")
    if enabled:
        _check(
            isinstance(agent.multi_agent_runtime, MultiAgentRuntime),
            "enabled smoke fixture did not construct the production MultiAgentRuntime",
        )
    else:
        _check(agent.multi_agent_runtime is None, "disabled smoke fixture constructed a runtime")
    return _AgentFixture(agent, memory, llm)


async def _run_turn(
    agent: Agent,
    prompt: str,
    history: list[dict] | None = None,
    *,
    request_id: str,
) -> str:
    return "".join([
        chunk
        async for chunk in agent.run_stream(
            prompt,
            history or [],
            request_id=request_id,
        )
    ])


async def _scenario_a(root: Path) -> SmokeScenarioResult:
    """A greeting cannot inherit stale tools, continuation, or delegation."""
    stale_call = (
        {"type": "tool_call", "index": 0, "id": "stale", "name": "create_task"},
        {
            "type": "tool_call_delta",
            "index": 0,
            "arguments": json.dumps({"title": "stale prior request"}),
        },
        {"type": "done"},
    )
    greeting = (
        {"type": "content", "text": "Hey!"},
        {"type": "done"},
    )
    fixture = _make_agent(
        root,
        "scenario-a",
        session_id="smoke-session-scenario-a",
        enabled=True,
        turns=(stale_call, greeting),
    )
    executed: list[str] = []

    async def forbidden_execute(name: str, _arguments: dict) -> str:
        executed.append(name)
        raise AssertionError("a stale greeting action reached the tool executor")

    fixture.agent.tool_executor.execute_async = forbidden_execute  # type: ignore[method-assign]
    try:
        answer = await _run_turn(
            fixture.agent,
            "hey",
            [{"role": "user", "content": "Create a durable task for the old request."}],
            request_id="smoke-request-scenario-a",
        )
        runtime = fixture.agent.multi_agent_runtime
        _check(isinstance(runtime, MultiAgentRuntime), "scenario A lost the real runtime")
        runs = runtime.list_runs(session_id="smoke-session-scenario-a")
        _check(executed == [], "scenario A executed a stale tool")
        _check(len(fixture.llm.calls) == 2, "scenario A did not reject and recover from the stale call")
        _check(
            all(call["tools"] == [] for call in fixture.llm.calls),
            "scenario A advertised a tool on a conversation-only turn",
        )
        _check("does not authorize" in answer, "scenario A did not report stale-call denial")
        _check("Hey!" in answer, "scenario A did not complete the greeting")
        _check(runs == [], "scenario A launched or persisted an agent run")
        evidence = {
            "entrypoint": "Agent.run_stream",
            "runtime": type(runtime).__name__,
            "advertised_tools": [call["tools"] for call in fixture.llm.calls],
            "executor_calls": tuple(executed),
            "run_count": len(runs),
            "answer": answer,
        }
    finally:
        await fixture.close()
    return SmokeScenarioResult(
        "A",
        "real Agent greeting; tools=0; stale execution=0; native runs=0",
        evidence,
    )


_RESEARCH_CLAIMS: dict[str, ResearchClaim] = {
    "researcher_1": ResearchClaim(
        claim="FastAPI emphasizes typed API development and generated interface documentation.",
        source_urls=("https://fastapi.tiangolo.com/",),
        evidence=("The official documentation describes type-hint-based APIs and generated docs.",),
        confidence=0.90,
        caveats=("Deployment and team constraints still determine suitability.",),
    ),
    "researcher_2": ResearchClaim(
        claim="Flask provides a compact core that applications can extend selectively.",
        source_urls=("https://flask.palletsprojects.com/",),
        evidence=("The official documentation presents Flask as a lightweight web framework.",),
        confidence=0.88,
        caveats=("Extension choices become part of the application architecture.",),
    ),
    "researcher_3": ResearchClaim(
        claim="Django provides an integrated framework with built-in application facilities.",
        source_urls=("https://docs.djangoproject.com/",),
        evidence=("The official documentation covers integrated model, admin, and request layers.",),
        confidence=0.89,
        caveats=("The integrated design may exceed the needs of a small service.",),
    ),
}


class _ResearchRuntimeAdapter:
    """Deterministic specialist boundary invoked by the real runtime."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.active_researchers = 0
        self.peak_researchers = 0

    @staticmethod
    def _metadata(task: AgentTask, context: AgentExecutionContext, tools: tuple[str, ...]) -> dict:
        root_run_id = str(context.run_metadata["root_run_id"])
        run_id = str(context.run_metadata["child_run_ids"][task.task_id])
        return {
            "run_id": run_id,
            "root_run_id": root_run_id,
            "parent_run_id": root_run_id,
            "iterations": 1,
            "tools": tools,
        }

    async def __call__(
        self,
        spec: AgentSpec,
        task: AgentTask,
        context: AgentExecutionContext,
    ) -> AgentOutput:
        self.calls.append({
            "task_id": task.task_id,
            "role": spec.name,
            "dependencies": tuple(context.dependency_results),
            "context_mode": context.context_mode.value,
        })
        if spec.name == "researcher":
            _check(not context.dependency_results, "researcher received unrelated dependency context")
            self.active_researchers += 1
            self.peak_researchers = max(self.peak_researchers, self.active_researchers)
            try:
                await asyncio.sleep(0.01)
            finally:
                self.active_researchers -= 1
            claim = _RESEARCH_CLAIMS[task.task_id]
            return AgentOutput(
                json.dumps({"claims": [claim.as_dict()]}, sort_keys=True),
                summary=claim.claim,
                metadata=self._metadata(task, context, ("web_search",)),
            )

        _check(spec.name == "synthesizer", f"unexpected research role {spec.name!r}")
        _check(
            tuple(context.dependency_results) == tuple(_RESEARCH_CLAIMS),
            "synthesizer did not receive the three ordered dependencies",
        )
        claims: list[ResearchClaim] = []
        for result in context.dependency_results.values():
            validation = parse_research_claims(result.content, require_structured=True)
            _check(validation.valid, f"invalid deterministic research result: {validation.issues}")
            claims.extend(validation.claims)
        return AgentOutput(
            json.dumps(
                {
                    "summary": "Choose according to the stated application constraints.",
                    "claims": [claim.as_dict() for claim in claims],
                    "disagreements": [],
                    "caveats": sorted({caveat for claim in claims for caveat in claim.caveats}),
                },
                sort_keys=True,
            ),
            summary="Synthesized three source-backed results without raising confidence.",
            metadata=self._metadata(task, context, ()),
        )


async def _scenarios_b_and_c(root: Path) -> tuple[SmokeScenarioResult, SmokeScenarioResult]:
    """Run native research, then introspect that same persisted run."""
    root_response = (
        {"type": "content", "text": "Recommendation: choose according to application constraints."},
        {"type": "done"},
    )
    fixture = _make_agent(
        root,
        "scenario-b-c",
        session_id=_RESEARCH_SESSION_ID,
        enabled=True,
        turns=(root_response,),
    )
    runtime = fixture.agent.multi_agent_runtime
    _check(isinstance(runtime, MultiAgentRuntime), "scenario B did not use the real runtime")
    adapter = _ResearchRuntimeAdapter()
    runtime.adapter = adapter  # type: ignore[assignment]
    prompt = (
        "Research FastAPI, Flask and Django in parallel using separate researchers, "
        "then synthesize a recommendation."
    )
    try:
        answer_b = await _run_turn(
            fixture.agent,
            prompt,
            request_id=_RESEARCH_REQUEST_ID,
        )
        run = runtime.get_latest_run(session_id=_RESEARCH_SESSION_ID)
        _check(run is not None, "scenario B did not persist a root run")
        manifest = run.get("manifest")
        _check(isinstance(manifest, dict), "scenario B runtime did not persist its manifest")
        root_run_id = str(manifest["root_run_id"])
        children = manifest["child_runs"]
        expected_waves = [
            ["researcher_1", "researcher_2", "researcher_3"],
            ["synthesis"],
        ]
        _check(root_run_id.startswith("ma_"), "scenario B root ID was not runtime-generated")
        _check(manifest["agent_count"] == 4, "scenario B runtime manifest count is not exact")
        _check(manifest["execution_waves"] == expected_waves, "scenario B runtime waves are wrong")
        _check(
            [child["role"] for child in children]
            == ["researcher", "researcher", "researcher", "synthesizer"],
            "scenario B runtime manifest roles are wrong",
        )
        _check(
            len({child["run_id"] for child in children}) == 4
            and all(str(child["run_id"]).startswith("agent_") for child in children),
            "scenario B child IDs were not uniquely runtime-generated",
        )
        _check(
            len({child["session_id"] for child in children}) == 4
            and all(child["session_id"] != _RESEARCH_SESSION_ID for child in children),
            "scenario B child sessions were not unique and bounded",
        )
        _check(adapter.peak_researchers == 3, "scenario B researcher wave did not overlap")
        observed_tools = {tool for child in children for tool in child["tools"]}
        _check(
            not observed_tools.intersection({"create_task", "run_task"}),
            "scenario B substituted a durable workflow for native agents",
        )
        source_urls = {url for claim in _RESEARCH_CLAIMS.values() for url in claim.source_urls}
        _check(all(url in answer_b for url in source_urls), "scenario B root answer dropped sources")
        _check(
            f"Verified native run {root_run_id}: 4 agents" in answer_b,
            "scenario B final truth did not come from the runtime manifest",
        )
        evidence_messages = "\n".join(
            str(message.get("content") or "")
            for call in fixture.llm.calls
            for message in call["messages"]
        )
        _check(
            "Verified Native Agent Execution Evidence" in evidence_messages,
            "scenario B runtime result was not supplied to root synthesis",
        )
        evidence_b = {
            "entrypoint": "Agent.run_stream -> MultiAgentRuntime.delegate",
            "root_run_id": root_run_id,
            "manifest": manifest,
            "answer": answer_b,
            "adapter_calls": tuple(adapter.calls),
            "peak_researchers": adapter.peak_researchers,
            "store_round_trip": runtime.get_run(root_run_id, session_id=_RESEARCH_SESSION_ID) is not None,
        }

        llm_calls_before = len(fixture.llm.calls)
        runs_before = len(runtime.list_runs(session_id=_RESEARCH_SESSION_ID))
        answer_c = await _run_turn(
            fixture.agent,
            "How many agents did you use, and how did you launch them?",
            request_id="smoke-request-scenario-c",
        )
        runs_after = len(runtime.list_runs(session_id=_RESEARCH_SESSION_ID))
        _check(len(fixture.llm.calls) == llm_calls_before, "scenario C called the root model")
        _check(runs_after == runs_before == 1, "scenario C launched a new agent run")
        _check(
            f"Verified native run {root_run_id}: 4 agents" in answer_c,
            "scenario C did not report the persisted manifest",
        )
        _check("root-owned native MultiAgentRuntime" in answer_c, "scenario C launch truth is missing")
        _check(
            runtime.get_run(root_run_id, session_id="smoke-session-other") is None,
            "scenario C leaked a run across session ownership",
        )
        evidence_c = {
            "entrypoint": "Agent.run_stream meta route",
            "root_run_id": root_run_id,
            "answer": answer_c,
            "llm_calls": len(fixture.llm.calls) - llm_calls_before,
            "new_runs": runs_after - runs_before,
            "cross_session_visible": False,
        }
    finally:
        await fixture.close()

    return (
        SmokeScenarioResult(
            "B",
            "real native runtime; generated root; agents=4; waves=3+1; sources=3; workflow substitutions=0",
            evidence_b,
        ),
        SmokeScenarioResult(
            "C",
            "real Agent meta route; persisted exact agents=4; model calls=0; new runs=0",
            evidence_c,
        ),
    )


async def _scenario_d(root: Path) -> SmokeScenarioResult:
    """Explicit delegation while disabled must terminate before model fallback."""
    fixture = _make_agent(
        root,
        "scenario-d",
        session_id="smoke-session-scenario-d",
        enabled=False,
        forbid_llm_calls=True,
    )
    try:
        answer = await _run_turn(
            fixture.agent,
            "Use four agents to compare these frameworks.",
            request_id="smoke-request-scenario-d",
        )
        lowered = answer.casefold()
        _check("disabled" in lowered and "no agents ran" in lowered, "scenario D was not honest")
        _check(fixture.llm.calls == [], "scenario D fell back to the root model")
        _check(fixture.agent.multi_agent_runtime is None, "scenario D created a disabled runtime")
        evidence = {
            "entrypoint": "Agent.run_stream",
            "runtime": None,
            "llm_calls": len(fixture.llm.calls),
            "answer": answer,
        }
    finally:
        await fixture.close()
    return SmokeScenarioResult(
        "D",
        "real Agent disabled path; claimed agents=0; model fallback=0; runtime=absent",
        evidence,
    )


class _BuilderRuntimeAdapter:
    """Same-path fake tool boundary invoked by real runtime builder tasks."""

    def __init__(self, runtime: MultiAgentRuntime, path: Path) -> None:
        self.runtime = runtime
        self.path = path
        self.calls: list[dict[str, Any]] = []
        self.active_mutations = 0
        self.peak_mutations = 0
        self.reviewer_verified = False

    @staticmethod
    def _metadata(task: AgentTask, context: AgentExecutionContext, tools: tuple[str, ...]) -> dict:
        root_run_id = str(context.run_metadata["root_run_id"])
        return {
            "run_id": str(context.run_metadata["child_run_ids"][task.task_id]),
            "root_run_id": root_run_id,
            "parent_run_id": root_run_id,
            "iterations": 1,
            "tools": tools,
        }

    async def __call__(
        self,
        spec: AgentSpec,
        task: AgentTask,
        context: AgentExecutionContext,
    ) -> AgentOutput:
        self.calls.append({
            "task_id": task.task_id,
            "role": spec.name,
            "dependencies": tuple(context.dependency_results),
        })
        if spec.name == "builder":
            async with self.runtime.resource_coordinator.acquire_call(
                "write_file", {"path": str(self.path)}
            ):
                self.active_mutations += 1
                self.peak_mutations = max(self.peak_mutations, self.active_mutations)
                try:
                    current = self.path.read_text(encoding="utf-8")
                    await asyncio.sleep(0.01)
                    self.path.write_text(current + f"{task.task_id}\n", encoding="utf-8")
                finally:
                    self.active_mutations -= 1
            return AgentOutput(
                f"Appended {task.task_id} through the production resource coordinator.",
                metadata=self._metadata(task, context, ("write_file",)),
            )

        _check(spec.name == "reviewer", f"unexpected builder role {spec.name!r}")
        _check(
            tuple(context.dependency_results) == ("builder_1", "builder_2"),
            "reviewer did not receive both builder results",
        )
        async with self.runtime.resource_coordinator.acquire_call(
            "read_file", {"path": str(self.path)}
        ):
            lines = self.path.read_text(encoding="utf-8").splitlines()
        _check(lines[0] == "base", "scenario E lost the base file content")
        _check(sorted(lines[1:]) == ["builder_1", "builder_2"], "scenario E lost a builder write")
        self.reviewer_verified = True
        return AgentOutput(
            "Reviewer verified the complete serialized patch.",
            metadata=self._metadata(task, context, ("read_file",)),
        )


async def _scenario_e(root: Path) -> SmokeScenarioResult:
    """Two real runtime builders conflict safely and a dependent reviewer runs."""
    fixture = _make_agent(
        root,
        "scenario-e",
        session_id="smoke-session-scenario-e",
        enabled=True,
        forbid_llm_calls=True,
        max_parallel_agents=2,
    )
    runtime = fixture.agent.multi_agent_runtime
    _check(isinstance(runtime, MultiAgentRuntime), "scenario E did not use the real runtime")
    path = root / "scenario-e" / "shared.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("base\n", encoding="utf-8")
    adapter = _BuilderRuntimeAdapter(runtime, path)
    runtime.adapter = adapter  # type: ignore[assignment]
    arguments = {
        "tasks": [
            {"task_id": "builder_1", "agent": "builder", "prompt": "Append the first marker."},
            {"task_id": "builder_2", "agent": "builder", "prompt": "Append the second marker."},
            {
                "task_id": "review",
                "agent": "reviewer",
                "prompt": "Verify both markers exactly once.",
                "depends_on": ["builder_1", "builder_2"],
                "allowed_context": ["task_dependencies"],
            },
        ],
        "context": "Only the bounded shared smoke file is in scope.",
    }
    turn = build_turn_execution_context(
        "Use multiple agents for two conflicting builders and a dependent reviewer.",
        request_id="smoke-request-scenario-e",
        session_id="smoke-session-scenario-e",
    )
    call = {
        "id": "scenario-e-native-delegation",
        "type": "function",
        "function": {
            "name": "delegate_tasks_parallel",
            "arguments": json.dumps(arguments),
        },
    }
    try:
        with fixture.agent.turn_scope(turn):
            tool_results = await fixture.agent.process_tool_calls_async([call])
        payload = json.loads(tool_results[0]["content"])
        _check(payload.get("status") == "succeeded", "scenario E native tool delegation failed")
        root_run_id = str(payload["root_run_id"])
        run = runtime.get_run(root_run_id, session_id="smoke-session-scenario-e")
        _check(run is not None, "scenario E runtime run was not persisted")
        manifest = run.get("manifest")
        _check(isinstance(manifest, dict), "scenario E runtime did not create a manifest")
        _check(
            manifest["execution_waves"] == [["builder_1", "builder_2"], ["review"]],
            "scenario E runtime waves did not preserve dependency order",
        )
        final_lines = path.read_text(encoding="utf-8").splitlines()
        _check(adapter.peak_mutations == 1, "scenario E same-path mutations overlapped")
        _check(sorted(final_lines[1:]) == ["builder_1", "builder_2"], "scenario E lost a write")
        _check(adapter.reviewer_verified, "scenario E reviewer did not verify the final file")
        _check(fixture.llm.calls == [], "scenario E bypass path called the root model")
        _check(manifest["agent_count"] == 3, "scenario E manifest count is not exact")
        workspace_records = run.get("metadata", {}).get("builder_workspaces", {})
        _check(
            set(workspace_records) == {"builder_1", "builder_2"},
            "scenario E runtime did not record builder workspace policy",
        )
        evidence = {
            "entrypoint": "Agent.process_tool_calls_async -> MultiAgentRuntime.execute_tool -> delegate",
            "root_run_id": root_run_id,
            "manifest": manifest,
            "peak_mutations": adapter.peak_mutations,
            "reviewer_verified": adapter.reviewer_verified,
            "final_lines": tuple(final_lines),
            "adapter_calls": tuple(adapter.calls),
            "workspace_records": workspace_records,
        }
    finally:
        await fixture.close()
    return SmokeScenarioResult(
        "E",
        "real Agent/runtime delegation; same-path builders serialized; peak mutations=1; reviewer=verified; lost writes=0",
        evidence,
    )


async def run_smoke_scenarios() -> tuple[SmokeScenarioResult, ...]:
    """Run production-path scenarios A-E, raising on any invariant."""
    with TemporaryDirectory(prefix="ares-native-multi-agent-smoke-") as directory:
        root = Path(directory)
        scenario_a = await _scenario_a(root)
        scenario_b, scenario_c = await _scenarios_b_and_c(root)
        scenario_d = await _scenario_d(root)
        scenario_e = await _scenario_e(root)
    return scenario_a, scenario_b, scenario_c, scenario_d, scenario_e


def main() -> int:
    """CLI entry point with a conventional success/failure exit status."""
    try:
        results = asyncio.run(run_smoke_scenarios())
    except Exception as exc:
        print(f"[FAIL] production-path multi-agent smoke: {type(exc).__name__}: {exc}")
        return 1
    for result in results:
        print(f"[PASS] Scenario {result.scenario}: {result.detail}")
    print(f"[PASS] production-path multi-agent smoke: {len(results)}/{len(results)} scenarios")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["SmokeScenarioResult", "main", "run_smoke_scenarios"]
