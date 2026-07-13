"""Glue between the watcher scheduler and the normal Ares agent/tool plane."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ares.actions import ActionLedger
    from ares.agent import Agent
    from ares.goals import GoalStore
    from ares.models import AppConfig
    from ares.watcher.models import Event, Monitor

from ares.watcher.service import WatcherService
from ares.watcher.database import resolve_watcher_database_path

logger = logging.getLogger(__name__)


class GoalWatcherBridge:
    """Fan watcher incidents into durable goal evidence without changing goal state."""

    def __init__(self, goal_store: "GoalStore", action_ledger: "ActionLedger | None" = None) -> None:
        self.goal_store = goal_store
        self.action_ledger = action_ledger

    def handle_event(self, event: "Event", monitor: "Monitor") -> list[dict[str, Any]]:
        linked_goals = self.goal_store.linked_goals(link_type="watcher", ref_id=monitor.id)
        signals: list[dict[str, Any]] = []
        for goal in linked_goals:
            try:
                signal = self.goal_store.record_watcher_signal(
                    int(goal["goal_id"]),
                    monitor.id,
                    event.change_summary or f"{monitor.name} detected {event.event_type.replace('_', ' ')}",
                    source_event_id=event.id,
                    event_type=event.event_type,
                    old_value=event.old_value,
                    new_value=event.new_value,
                    severity=event.severity,
                    created_at=event.created_at.isoformat(),
                    metadata={"watcher_name": monitor.name, "watcher_type": monitor.type},
                )
                signal["goal_title"] = goal["title"]
                signal["goal_status"] = goal["status"]
                signals.append(signal)
                if signal.get("created") and self.action_ledger is not None:
                    self.action_ledger.record(
                        "watcher_goal_signal",
                        target=f"goal #{goal['goal_id']} · {goal['title']}",
                        summary="A linked watcher produced new goal evidence requiring user review.",
                        tool_name="watcher_goal_signal",
                        tags=["goal", "watcher", str(event.severity)],
                        created_at=event.created_at.isoformat(),
                    )
            except Exception:
                logger.exception(
                    "Could not record watcher event %s for linked goal #%s",
                    event.id, goal.get("goal_id"),
                )
        return signals


async def execute_agent_tool(agent: "Agent", tool_name: str, arguments: dict[str, Any]) -> str:
    """Execute one watcher workflow step through the same guarded Ares path as chat."""
    call = {
        "id": "watcher-background-step",
        "type": "function",
        "function": {"name": tool_name, "arguments": json.dumps(arguments, ensure_ascii=False)},
    }
    results = await agent.process_tool_calls_async([call])
    if not results:
        return "Error: Ares returned no result for the watcher workflow step."
    return str(results[0].get("content") or "")


def create_agent_watcher_service(config: "AppConfig", agent: "Agent") -> WatcherService:
    """Create and attach the one watcher service owned by an Ares runtime."""
    watcher = config.watcher

    async def runner(tool_name: str, arguments: dict[str, Any]) -> str:
        return await execute_agent_tool(agent, tool_name, arguments)

    goal_store = getattr(agent, "goal_store", None)
    action_ledger = getattr(getattr(agent, "tool_executor", None), "action_ledger", None)
    goal_bridge = GoalWatcherBridge(goal_store, action_ledger) if goal_store is not None else None

    service = WatcherService(
        resolve_watcher_database_path(config),
        notification_settings=watcher.notifications,
        max_concurrency=watcher.max_concurrency,
        poll_seconds=watcher.poll_seconds,
        goal_signal_handler=goal_bridge.handle_event if goal_bridge is not None else None,
        tool_runner=runner if watcher.tool_monitors_enabled else None,
        allow_mutating_tools=watcher.allow_mutating_tool_steps,
        max_tool_steps=watcher.max_tool_steps,
        max_tool_output_chars=watcher.max_tool_output_chars,
    )
    executor = getattr(agent, "tool_executor", None)
    setter = getattr(executor, "set_watcher_service", None)
    if setter is not None:
        setter(service)
    return service


__all__ = ["GoalWatcherBridge", "create_agent_watcher_service", "execute_agent_tool"]
