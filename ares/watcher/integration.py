"""Glue between the watcher scheduler and the normal Ares agent/tool plane."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ares.agent import Agent
    from ares.models import AppConfig

from ares.watcher.service import WatcherService
from ares.watcher.database import resolve_watcher_database_path


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

    service = WatcherService(
        resolve_watcher_database_path(config),
        notification_settings=watcher.notifications,
        max_concurrency=watcher.max_concurrency,
        poll_seconds=watcher.poll_seconds,
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


__all__ = ["create_agent_watcher_service", "execute_agent_tool"]
