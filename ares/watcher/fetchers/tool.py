"""Fetcher that turns existing Ares tools into durable watcher signals.

Tool-backed watchers are intentionally observation-first.  Read operations and
browser navigation/snapshots are allowed by default; clicks, typing, sending,
deleting, shell execution, and other consequential actions require both a
global runtime opt-in and an explicit per-monitor opt-in.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import Awaitable, Callable
from typing import Any

from ares.watcher.fetchers.base import BaseFetcher, FetchResult, FetcherError
from ares.watcher.fetchers.custom import json_path_get


ToolRunner = Callable[[str, dict[str, Any]], Awaitable[Any]]

_LOCAL_OBSERVATION_TOOLS = {
    "fetch_url", "web_search", "read_file", "search_files", "list_directory",
    "get_file_info", "glob_pattern", "disk_usage", "checksum", "find_duplicates",
    "tail_file", "head_file", "count_lines", "file_tree", "search_memory",
    "search_person", "search_actions", "list_tasks", "get_task_status",
    "list_goals", "get_goal_status", "phone_status", "phone_get_notifications",
    "phone_search_contact", "telephony_status", "telephony_get_call",
    "telephony_list_calls", "telephony_list_contacts", "get_current_datetime",
}
_MANAGEMENT_PREFIXES = (
    "create_watcher", "list_watchers", "get_watcher", "update_watcher",
    "delete_watcher", "run_watcher", "pause_watcher", "resume_watcher",
    "list_watcher", "acknowledge_watcher", "get_watcher_capabilities",
)
_MUTATING_TOKENS = {
    "click", "type", "fill", "press", "select", "drag", "upload", "send",
    "delete", "remove", "write", "edit", "update", "create", "install",
    "execute", "run_command", "terminal", "call", "hangup", "transfer",
    "launch", "save", "move", "copy", "generate", "submit", "post",
}
_OBSERVATION_TOKENS = {
    "get", "list", "read", "search", "find", "fetch", "snapshot", "screenshot",
    "status", "inspect", "query", "view", "health", "info", "notifications",
}


def validate_tool_step(
    tool_name: str,
    *,
    allow_navigation: bool = False,
    allow_mutating: bool = False,
) -> None:
    """Reject recursive or consequential background tool calls by default."""
    name = str(tool_name or "").strip()
    lowered = name.casefold()
    if not name:
        raise FetcherError("Every watcher workflow step requires a tool_name")
    if lowered.startswith(_MANAGEMENT_PREFIXES):
        raise FetcherError("Watcher workflows cannot recursively call watcher management tools")

    if lowered.startswith("mcp__"):
        leaf = lowered.rsplit("__", 1)[-1]
        tokens = set(filter(None, re.split(r"[^a-z0-9]+", leaf)))
        if "navigate" in tokens and allow_navigation:
            return
        if tokens & _MUTATING_TOKENS and not allow_mutating:
            raise FetcherError(
                f"Background MCP step {name!r} can change external state; "
                "enable mutating tool steps globally and on this watcher to allow it"
            )
        if tokens & _OBSERVATION_TOKENS:
            return
        if not allow_mutating:
            raise FetcherError(f"MCP step {name!r} is not recognizably read-only")
        return

    if lowered in _LOCAL_OBSERVATION_TOOLS:
        return
    if not allow_mutating:
        raise FetcherError(f"Local tool {name!r} is not approved for background observation")


def browser_steps(target: str, config: dict[str, Any]) -> list[dict[str, Any]]:
    """Build the normal authenticated-browser observation recipe."""
    steps: list[dict[str, Any]] = []
    if target and config.get("navigate", True):
        steps.append({
            "tool_name": str(config.get("navigate_tool") or "mcp__playwright__browser_navigate"),
            "arguments": {"url": target},
            "allow_navigation": True,
        })
    steps.append({
        "tool_name": str(config.get("snapshot_tool") or "mcp__playwright__browser_snapshot"),
        "arguments": dict(config.get("snapshot_arguments") or {}),
    })
    return steps


def _substitute(value: Any, context: dict[str, str]) -> Any:
    if isinstance(value, dict):
        return {str(key): _substitute(item, context) for key, item in value.items()}
    if isinstance(value, list):
        return [_substitute(item, context) for item in value]
    if not isinstance(value, str):
        return value
    if value in context:
        return context[value]
    rendered = value
    for token, replacement in context.items():
        rendered = rendered.replace(token, replacement)
    return rendered


def _decode_result(result: Any) -> Any:
    if not isinstance(result, str):
        return result
    stripped = result.strip()
    if stripped.startswith("Error:"):
        raise FetcherError(stripped)
    try:
        return json.loads(stripped)
    except (json.JSONDecodeError, TypeError):
        return result


def _extract(value: Any, config: dict[str, Any]) -> Any:
    extraction = config.get("extract") or {}
    if not isinstance(extraction, dict) or not extraction:
        return value
    if extraction.get("json_path"):
        value = json_path_get(value, str(extraction["json_path"]))
    if extraction.get("regex"):
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
        match = re.search(str(extraction["regex"]), text, flags=re.MULTILINE | re.DOTALL)
        if not match:
            raise FetcherError("The configured result extraction regex did not match")
        group = extraction.get("group", 1 if match.lastindex else 0)
        value = match.group(group)
    return value


class ToolWorkflowFetcher(BaseFetcher):
    """Execute a bounded workflow through Ares' existing local/MCP tools."""

    def __init__(
        self,
        runner: ToolRunner | None,
        *,
        browser: bool = False,
        global_allow_mutating: bool = False,
        max_steps: int = 8,
        max_output_chars: int = 2_000_000,
    ) -> None:
        self.runner = runner
        self.browser = browser
        self.global_allow_mutating = bool(global_allow_mutating)
        self.max_steps = max(1, int(max_steps))
        self.max_output_chars = max(1_000, int(max_output_chars))
        self._execution_lock = asyncio.Lock()

    async def fetch(self, target: str, config: dict[str, Any] | None = None) -> FetchResult:
        # A Playwright MCP server usually owns one page/session. Serializing the
        # complete recipe prevents two due monitors from navigating that shared
        # session underneath each other's snapshots.
        if self.browser:
            async with self._execution_lock:
                return await self._fetch(target, config)
        return await self._fetch(target, config)

    async def _fetch(self, target: str, config: dict[str, Any] | None = None) -> FetchResult:
        cfg, started = config or {}, time.perf_counter()
        if self.runner is None:
            return FetchResult(False, error="Ares tool integrations are not attached to this watcher runtime")
        try:
            raw_steps = cfg.get("steps")
            if self.browser and not raw_steps:
                raw_steps = browser_steps(target, cfg)
            elif not raw_steps and cfg.get("tool_name"):
                raw_steps = [{"tool_name": cfg["tool_name"], "arguments": cfg.get("arguments") or {}}]
            if not isinstance(raw_steps, list) or not raw_steps:
                raise FetcherError("Tool watchers require at least one configured workflow step")
            if len(raw_steps) > min(self.max_steps, int(cfg.get("max_steps", self.max_steps))):
                raise FetcherError(f"Watcher workflow exceeds the {self.max_steps}-step safety limit")

            per_monitor_mutating = bool(cfg.get("allow_mutating_tools"))
            allow_mutating = self.global_allow_mutating and per_monitor_mutating
            context = {"${target}": target, "${previous}": ""}
            value: Any = None
            trace: list[dict[str, Any]] = []
            for index, raw_step in enumerate(raw_steps):
                if not isinstance(raw_step, dict):
                    raise FetcherError(f"Workflow step {index + 1} must be an object")
                tool_name = str(raw_step.get("tool_name") or "")
                allow_navigation = bool(raw_step.get("allow_navigation")) or (
                    self.browser and tool_name.casefold().endswith("browser_navigate")
                )
                validate_tool_step(tool_name, allow_navigation=allow_navigation, allow_mutating=allow_mutating)
                arguments = _substitute(dict(raw_step.get("arguments") or {}), context)
                step_started = time.perf_counter()
                raw_result = await asyncio.wait_for(
                    self.runner(tool_name, arguments),
                    timeout=max(1.0, min(float(raw_step.get("timeout", cfg.get("timeout", 30))), 120.0)),
                )
                text_result = raw_result if isinstance(raw_result, str) else json.dumps(raw_result, ensure_ascii=False, default=str)
                if len(text_result) > self.max_output_chars:
                    raise FetcherError(f"Tool output exceeded the {self.max_output_chars}-character safety limit")
                value = _decode_result(raw_result)
                context["${previous}"] = text_result
                context[f"${{step.{index}}}"] = text_result
                trace.append({
                    "index": index,
                    "tool_name": tool_name,
                    "elapsed_ms": round((time.perf_counter() - step_started) * 1000),
                    "output_chars": len(text_result),
                })

            value = _extract(value, cfg)
            serialized = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
            return FetchResult(
                True,
                value,
                {
                    "source": "ares_browser" if self.browser else "ares_tool_workflow",
                    "steps": trace,
                    "bytes": len(serialized.encode("utf-8")),
                },
                elapsed_ms=round((time.perf_counter() - started) * 1000),
            )
        except (FetcherError, asyncio.TimeoutError, ValueError, TypeError) as exc:
            error = "Tool workflow timed out" if isinstance(exc, asyncio.TimeoutError) else str(exc)
            return FetchResult(False, error=error, elapsed_ms=round((time.perf_counter() - started) * 1000))


__all__ = ["ToolRunner", "ToolWorkflowFetcher", "browser_steps", "validate_tool_step"]
