"""Shared, presentation-safe summaries for multi-agent operational surfaces."""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime
from typing import Any, Iterable


ACTIVE_STATUSES = frozenset({"queued", "running"})
FAILED_STATUSES = frozenset({"failed", "timed_out", "blocked"})


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def elapsed_seconds(run: dict[str, Any], *, now: datetime | None = None) -> int:
    start = _parse_time(run.get("started_at") or run.get("created_at"))
    if start is None:
        return 0
    end = _parse_time(run.get("completed_at")) or now or datetime.now(UTC)
    return max(0, round((end - start).total_seconds()))


def elapsed_label(run: dict[str, Any]) -> str:
    seconds = elapsed_seconds(run)
    if seconds < 60:
        return f"{seconds}s"
    minutes, remainder = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {remainder}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m"


def summarize_runs(runs: Iterable[dict[str, Any]]) -> dict[str, Any]:
    values = list(runs)
    workers = [child for run in values for child in (run.get("children") or [])]
    run_counts = Counter(str(run.get("status") or "unknown") for run in values)
    worker_counts = Counter(str(worker.get("status") or "unknown") for worker in workers)
    return {
        "runs": len(values),
        "active_runs": sum(run_counts[status] for status in ACTIVE_STATUSES),
        "workers": len(workers),
        "active_workers": sum(worker_counts[status] for status in ACTIVE_STATUSES),
        "run_counts": dict(run_counts),
        "worker_counts": dict(worker_counts),
    }


def active_runs(runs: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return [run for run in runs if str(run.get("status") or "") in ACTIVE_STATUSES]


def status_mark(status: Any) -> str:
    return {
        "queued": "○",
        "running": "●",
        "succeeded": "✓",
        "failed": "✗",
        "timed_out": "⌛",
        "blocked": "⊘",
        "cancelled": "–",
    }.get(str(status or "").casefold(), "·")


def one_line(value: Any, limit: int = 120) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def telegram_overview(
    *, enabled: bool, agents: Iterable[dict[str, Any]], runs: Iterable[dict[str, Any]]
) -> str:
    run_values = list(runs)
    specialists = list(agents)
    summary = summarize_runs(run_values)
    active = active_runs(run_values)
    lines = [
        "Ares specialist runtime",
        f"Mode: {'enabled' if enabled else 'disabled'}",
        f"Teams: {summary['active_runs']} active · {summary['runs']} recent",
        f"Workers: {summary['active_workers']} active · {summary['workers']} tracked",
        f"Roles: {', '.join(str(item.get('name') or '') for item in specialists) or 'none'}",
    ]
    if active:
        lines.append("\nActive teams")
        for run in active[:8]:
            children = run.get("children") or []
            active_count = sum(str(item.get("status") or "") in ACTIVE_STATUSES for item in children)
            lines.append(
                f"● {str(run.get('run_id') or '')[:19]} · {active_count}/{len(children)} working · "
                f"{elapsed_label(run)}\n  {one_line(run.get('prompt_summary') or run.get('activity'), 110)}"
            )
    else:
        lines.append("\nNo specialist teams are currently running.")
    lines.append("\nUse /agents active, /agents runs, or /agents show RUN_ID.")
    return "\n".join(lines)


def telegram_run(run: dict[str, Any], *, include_results: bool = True) -> str:
    lines = [
        f"Agent team · {run.get('run_id', '')}",
        f"{status_mark(run.get('status'))} {run.get('status', 'unknown')} · {elapsed_label(run)}",
        one_line(run.get("prompt_summary") or run.get("activity") or "Delegated task", 180),
    ]
    children = run.get("children") or []
    if children:
        lines.append("\nWorkers")
    for child in children:
        status = str(child.get("status") or "unknown")
        role = str(child.get("agent_role") or "specialist")
        task = str(child.get("task_id") or "task")
        activity = child.get("activity") or child.get("current_tool") or child.get("result_summary")
        if not activity and status in FAILED_STATUSES:
            activity = child.get("error_summary")
        dependency = ", ".join(str(item) for item in (child.get("dependencies") or []))
        lines.append(f"{status_mark(status)} {role} · {task} · {status}")
        if dependency:
            lines.append(f"  after: {dependency}")
        if activity:
            lines.append(f"  {one_line(activity, 150)}")
        if include_results and status == "succeeded" and child.get("result_summary"):
            lines.append(f"  result: {one_line(child['result_summary'], 150)}")
        artifacts = child.get("artifacts") or []
        if artifacts:
            lines.append(f"  artifacts: {', '.join(one_line(item.get('path'), 70) for item in artifacts[:3])}")
    return "\n".join(lines)
