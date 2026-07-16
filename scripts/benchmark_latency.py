"""Deterministic offline latency smoke benchmark for Ares.

The benchmark deliberately uses a local scripted provider and fake tools, so it
can compare branches without credentials, network access, or personal Ares
data.  Wall-clock TTFT and total time are measured by this script itself;
agent-provided latency records are displayed only as supplemental diagnostics.

Run from the repository root:

    python scripts/benchmark_latency.py --iterations 5
"""

from __future__ import annotations

import argparse
import asyncio
from contextlib import contextmanager
from dataclasses import asdict, dataclass
import json
from pathlib import Path
import statistics
import tempfile
import time
from typing import Any, AsyncIterator

from ares.agent import Agent
from ares.commitments import CommitmentStore
from ares.embeddings import EmbeddingProvider
from ares.goals import GoalStore
from ares.memory import MemoryStore
from ares.models import AppConfig


_FIRST_PROVIDER_DELAY_SECONDS = 0.025
# Keep the final provider completion well after the first delta.  On an old
# buffering implementation this produces roughly 125 ms wall TTFT, while a
# true streaming implementation can surface the first delta near 50 ms.
_RESPONSE_TAIL_DELAY_SECONDS = 0.100
_LOCAL_TOOL_DELAY_SECONDS = 0.035
_MCP_TOOL_DELAY_SECONDS = 0.045
_REFLECTION_DELAY_SECONDS = 0.120

_EMPTY_REFLECTION = json.dumps({
    "new_memories": [],
    "updated_memories": [],
    "new_goals": [],
    "goal_progress": [],
    "completed_goals": [],
    "profile_updates": [],
    "commitments": [],
    "follow_up_opportunities": [],
    "follow_up_resolutions": [],
})


@dataclass
class WallMeasurement:
    """Timing captured independently of Ares's optional tracker."""

    context_ms: float | None
    provider_ttft_ms: float | None
    wall_ttft_ms: float | None
    total_ms: float
    tracker_ares_ttft_ms: float | None


@dataclass
class BenchmarkRow:
    scenario: str
    context_ms: float | None
    provider_ttft_ms: float | None
    wall_ttft_ms: float | None
    total_ms: float
    tracker_ares_ttft_ms: float | None


class ScriptedLLM:
    """A local stream source with fixed provider and reflection delays."""

    def __init__(
        self,
        config: AppConfig,
        stream_plans: list[list[tuple[float, dict[str, Any]]]],
        *,
        reflection_delay_seconds: float = 0.0,
    ) -> None:
        self.config = config
        self.model = config.model
        self._stream_plans = list(stream_plans)
        self.provider_first_event_at: float | None = None
        self.reflection_started = asyncio.Event()
        self._reflection_delay_seconds = reflection_delay_seconds
        self._reflection_calls = 0

    def begin_request(self) -> None:
        """Reset the first-provider marker once per user-facing request."""
        self.provider_first_event_at = None

    def queue_stream_plans(self, stream_plans: list[list[tuple[float, dict[str, Any]]]]) -> None:
        """Append deterministic model turns for the next benchmark request."""
        self._stream_plans.extend(stream_plans)

    async def chat_stream(
        self, _messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        del tools
        if not self._stream_plans:
            raise RuntimeError("benchmark provider received more turns than planned")
        plan = self._stream_plans.pop(0)
        for delay_seconds, event in plan:
            if delay_seconds:
                await asyncio.sleep(delay_seconds)
            if self.provider_first_event_at is None:
                self.provider_first_event_at = time.monotonic()
            yield dict(event)

    async def chat(
        self, _messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None,
    ) -> dict[str, str]:
        """Return a valid no-op reflection response after a controlled delay."""
        del tools
        self._reflection_calls += 1
        self.reflection_started.set()
        if self._reflection_calls == 1 and self._reflection_delay_seconds:
            await asyncio.sleep(self._reflection_delay_seconds)
        return {"content": _EMPTY_REFLECTION}

    async def close(self) -> None:
        return None


class BenchmarkToolExecutor:
    """Minimal local executor that leaves benchmark work fully offline."""

    def __init__(
        self,
        *,
        goal_store: GoalStore | None = None,
        commitment_store: CommitmentStore | None = None,
    ) -> None:
        self.session_store = None
        self.people_store = None
        self.action_ledger = None
        self.task_store = None
        self.goal_store = goal_store
        self.commitment_store = commitment_store
        self.follow_up_store = None
        self.workflow_runner = None
        self.skill_manager = None

    @contextmanager
    def session_scope(self, _session_id: str | None):
        yield

    def set_session_id(self, _session_id: str | None) -> None:
        return None

    async def execute_async(self, _tool_name: str, _arguments: dict[str, Any]) -> str:
        await asyncio.sleep(_LOCAL_TOOL_DELAY_SECONDS)
        return "benchmark local tool result"

    def execute(self, _tool_name: str, _arguments: dict[str, Any]) -> str:
        return "benchmark local tool result"

    def record_external_action(
        self, _tool_name: str, _arguments: dict[str, Any], _result: str,
    ) -> None:
        return None

    def close(self) -> None:
        return None


class BenchmarkMCPManager:
    """Connected-tool stand-in; it never reaches a remote MCP server."""

    tool_definitions: list[dict[str, Any]] = []

    async def call_tool(self, _tool_name: str, _arguments: dict[str, Any]) -> str:
        await asyncio.sleep(_MCP_TOOL_DELAY_SECONDS)
        return "benchmark MCP tool result"

    def readiness_report(self) -> dict[str, Any]:
        return {"servers": {"benchmark": {"ready": True}}}


def _text_plan(answer: str) -> list[tuple[float, dict[str, Any]]]:
    """Give no-tool answers a visible first delta and a delayed completion."""
    return [
        (_FIRST_PROVIDER_DELAY_SECONDS, {"type": "content", "text": answer[:6]}),
        (_RESPONSE_TAIL_DELAY_SECONDS, {"type": "content", "text": answer[6:]}),
        (0.0, {"type": "done"}),
    ]


def _tool_plan(tool_name: str, arguments: dict[str, Any], answer: str) -> list[list[tuple[float, dict[str, Any]]]]:
    encoded_arguments = json.dumps(arguments, separators=(",", ":"))
    return [
        [
            (_FIRST_PROVIDER_DELAY_SECONDS, {
                "type": "tool_call", "index": 0, "id": "benchmark-call", "name": tool_name,
            }),
            (0.0, {"type": "tool_call_delta", "index": 0, "arguments": encoded_arguments}),
            (0.0, {"type": "done"}),
        ],
        _text_plan(answer),
    ]


def _config(data_dir: Path, *, reflection_enabled: bool) -> AppConfig:
    config = AppConfig(
        data_dir=str(data_dir),
        project_context_enabled=False,
        skills_enabled=False,
        skill_auto_suggest=False,
        mcp_servers=[],
    )
    config.reflection.enabled = reflection_enabled
    config.multi_agent.enabled = False
    return config


def _create_agent(
    directory: Path,
    *,
    stream_plans: list[list[tuple[float, dict[str, Any]]]],
    memory_fact: str | None = None,
    reflection_enabled: bool = False,
) -> tuple[Agent, MemoryStore, ScriptedLLM]:
    directory.mkdir(parents=True, exist_ok=True)
    config = _config(directory / "ares-data", reflection_enabled=reflection_enabled)
    memory = MemoryStore(
        directory / "ares.db",
        embedding_provider=EmbeddingProvider(backend="hash"),
    )
    if memory_fact:
        memory.store(memory_fact, category="preference")

    goals = GoalStore(directory / "ares.db", connection=memory.conn) if reflection_enabled else None
    commitments = (
        CommitmentStore(directory / "ares.db", connection=memory.conn)
        if reflection_enabled else None
    )
    executor = BenchmarkToolExecutor(goal_store=goals, commitment_store=commitments)
    llm = ScriptedLLM(
        config,
        stream_plans,
        reflection_delay_seconds=_REFLECTION_DELAY_SECONDS if reflection_enabled else 0.0,
    )
    agent = Agent(
        memory_store=memory,
        config=config,
        mcp_manager=BenchmarkMCPManager(),
        tool_executor=executor,
        llm_client=llm,
        session_id="benchmark-session",
    )
    # The benchmark supplies only harmless local fake calls.  This avoids
    # measuring authorization policy instead of the tool dispatch latency.
    agent._authorize_tool = lambda _name, _arguments: None  # type: ignore[method-assign]
    return agent, memory, llm


def _tracker_ares_ttft(agent: Agent) -> float | None:
    """Read optional agent metrics without making the benchmark depend on them."""
    records = getattr(agent, "recent_latency_metrics", None)
    if not records:
        return None
    try:
        record = records[-1]
        metrics = record.get("metrics", {}) if isinstance(record, dict) else {}
        value = metrics.get("ares_ttft_ms") if isinstance(metrics, dict) else None
        return float(value) if value is not None else None
    except (IndexError, TypeError, ValueError):
        return None


def _is_internal_tool_event(chunk: str) -> bool:
    return chunk.startswith("[tool")


async def _measure_response(agent: Agent, prompt: str) -> WallMeasurement:
    """Measure one run, wrapping context construction without relying on tracing."""
    context_ms: float | None = None
    original_get_context = agent.get_context

    def timed_get_context(*args: Any, **kwargs: Any) -> str:
        nonlocal context_ms
        started_at = time.monotonic()
        try:
            return original_get_context(*args, **kwargs)
        finally:
            context_ms = (time.monotonic() - started_at) * 1000.0

    agent.get_context = timed_get_context  # type: ignore[method-assign]
    begin_request = getattr(agent.llm, "begin_request", None)
    if callable(begin_request):
        begin_request()
    started_at = time.monotonic()
    first_visible_at: float | None = None
    try:
        async for chunk in agent.run_stream(prompt, []):
            if chunk and first_visible_at is None and not _is_internal_tool_event(chunk):
                first_visible_at = time.monotonic()
    finally:
        agent.get_context = original_get_context  # type: ignore[method-assign]
    finished_at = time.monotonic()
    provider_at = getattr(agent.llm, "provider_first_event_at", None)
    return WallMeasurement(
        context_ms=context_ms,
        provider_ttft_ms=(provider_at - started_at) * 1000.0 if provider_at is not None else None,
        wall_ttft_ms=(first_visible_at - started_at) * 1000.0 if first_visible_at is not None else None,
        total_ms=(finished_at - started_at) * 1000.0,
        tracker_ares_ttft_ms=_tracker_ares_ttft(agent),
    )


async def _run_reflection_overlap_scenario(directory: Path) -> WallMeasurement:
    agent, memory, llm = _create_agent(
        directory,
        stream_plans=[
            _text_plan("First reply."),
            _text_plan("Second reply."),
        ],
        reflection_enabled=True,
    )
    try:
        await _measure_response(agent, "I prefer concise benchmark reports.")
        await asyncio.wait_for(llm.reflection_started.wait(), timeout=1.0)
        return await _measure_response(agent, "What should I do next?")
    finally:
        await agent.close()
        memory.close()


def _median(values: list[float | None]) -> float | None:
    present = [value for value in values if value is not None]
    return statistics.median(present) if present else None


def _row_from_samples(name: str, samples: list[WallMeasurement]) -> BenchmarkRow:
    return BenchmarkRow(
        scenario=name,
        context_ms=_median([sample.context_ms for sample in samples]),
        provider_ttft_ms=_median([sample.provider_ttft_ms for sample in samples]),
        wall_ttft_ms=_median([sample.wall_ttft_ms for sample in samples]),
        total_ms=float(_median([sample.total_ms for sample in samples]) or 0.0),
        tracker_ares_ttft_ms=_median([sample.tracker_ares_ttft_ms for sample in samples]),
    )


async def _run_scenarios(iterations: int) -> list[BenchmarkRow]:
    standard_scenarios: list[tuple[str, str, list[list[tuple[float, dict[str, Any]]]]]] = [
        (
            "simple no-tool response",
            "Give a short greeting.",
            [_text_plan("Hello from Ares.")],
        ),
        (
            "memory-aware response",
            "What theme do I prefer?",
            [_text_plan("You prefer a dark theme.")],
        ),
        (
            "explicit recall",
            "What did I say yesterday?",
            [_text_plan("You mentioned a release.")],
        ),
        (
            "local tool call",
            "Read the local benchmark file.",
            _tool_plan("read_file", {"path": "benchmark.txt"}, "Local tool finished."),
        ),
        (
            "MCP tool call",
            "Use the connected benchmark service.",
            _tool_plan("mcp__benchmark__echo", {"text": "ping"}, "MCP tool finished."),
        ),
    ]

    rows: list[BenchmarkRow] = []
    with tempfile.TemporaryDirectory(prefix="ares-latency-benchmark-") as temporary:
        root = Path(temporary)
        # Reuse one no-reflection agent for the first five scenarios.  This
        # avoids measuring repeated SQLite/bootstrap work instead of request
        # latency and makes the default multi-iteration run practical locally.
        agent, memory, llm = _create_agent(
            root / "standard",
            stream_plans=[],
            memory_fact="User prefers a dark theme and mentioned a release yesterday.",
        )
        try:
            for name, prompt, stream_plans in standard_scenarios:
                samples: list[WallMeasurement] = []
                for _ in range(iterations):
                    llm.queue_stream_plans(stream_plans)
                    samples.append(await _measure_response(agent, prompt))
                rows.append(_row_from_samples(name, samples))
        finally:
            await agent.close()
            memory.close()

        reflection_samples = [
            await _run_reflection_overlap_scenario(root / f"reflection-{iteration}")
            for iteration in range(iterations)
        ]
        rows.append(_row_from_samples("second turn during reflection", reflection_samples))
    return rows


def _format_ms(value: float | None) -> str:
    return "-" if value is None else f"{value:.1f}"


def _render_table(rows: list[BenchmarkRow]) -> str:
    headers = ["Scenario", "Context ms", "Provider TTFT", "Ares TTFT", "Total", "Tracked Ares TTFT"]
    values = [
        [
            row.scenario,
            _format_ms(row.context_ms),
            _format_ms(row.provider_ttft_ms),
            _format_ms(row.wall_ttft_ms),
            _format_ms(row.total_ms),
            _format_ms(row.tracker_ares_ttft_ms),
        ]
        for row in rows
    ]
    widths = [max(len(headers[index]), *(len(row[index]) for row in values)) for index in range(len(headers))]
    separator = "|".join("-" * (width + 2) for width in widths)
    lines = [
        "| " + " | ".join(header.ljust(widths[index]) for index, header in enumerate(headers)) + " |",
        "|" + separator + "|",
    ]
    lines.extend(
        "| " + " | ".join(value.ljust(widths[index]) for index, value in enumerate(row)) + " |"
        for row in values
    )
    return "\n".join(lines)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run deterministic offline Ares latency scenarios.")
    parser.add_argument("--iterations", type=int, default=3, help="Measured runs per scenario (default: 3).")
    parser.add_argument("--json", type=Path, help="Optional path for machine-readable median results.")
    args = parser.parse_args()
    if args.iterations < 1:
        parser.error("--iterations must be at least 1")
    return args


def main() -> int:
    args = _parse_args()
    rows = asyncio.run(_run_scenarios(args.iterations))
    print("Offline synthetic latency benchmark (wall TTFT/total are tracker-independent)")
    print(_render_table(rows))
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(
            json.dumps([asdict(row) for row in rows], indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"\nWrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
