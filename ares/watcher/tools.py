"""First-class Ares tool handlers for creating and operating watchers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable
from uuid import uuid4

from ares.watcher.database import WatcherDatabase
from ares.watcher.fetchers.base import FetcherError
from ares.watcher.fetchers.tool import browser_steps, validate_tool_step
from ares.watcher.models import Monitor

if TYPE_CHECKING:
    from ares.watcher.service import WatcherService


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)


def _browser_preset(name: str) -> tuple[str | None, dict[str, Any]]:
    preset = str(name or "").casefold()
    if preset in {"instagram_dm", "instagram_dms", "dm", "dms"}:
        return "https://www.instagram.com/direct/inbox/", {
            "preset": "instagram_dm",
            "navigate": True,
            "change_detection": "diff",
            "ignore_patterns": [
                r"\b\d+\s*(?:s|m|h|d|w)\b",
                r"\bactive\s+(?:now|\d+\s*(?:m|h)\s*ago)\b",
            ],
        }
    if preset in {"browser", "browser_page", "authenticated_page"}:
        return None, {"preset": "browser_page", "navigate": True, "change_detection": "diff"}
    return None, {}


class WatcherToolHandlers:
    """Manage the shared watcher DB and delegate live checks to the runtime service."""

    def __init__(
        self,
        database_path: str | Path,
        *,
        service: WatcherService | None = None,
        tool_monitors_enabled: bool = True,
        allow_mutating_tool_steps: bool = False,
        capabilities_provider: Callable[[], list[str]] | None = None,
        goal_store: Any | None = None,
    ) -> None:
        self.database_path = Path(database_path).expanduser()
        self.service = service
        self.tool_monitors_enabled = bool(tool_monitors_enabled)
        self.allow_mutating_tool_steps = bool(allow_mutating_tool_steps)
        self.capabilities_provider = capabilities_provider or (lambda: [])
        self.goal_store = goal_store
        self._owned_db: WatcherDatabase | None = None

    @property
    def db(self) -> WatcherDatabase:
        if self.service is not None:
            return self.service.db
        if self._owned_db is None:
            self._owned_db = WatcherDatabase(self.database_path)
        return self._owned_db

    def set_service(self, service: WatcherService | None) -> None:
        if self._owned_db is not None:
            self._owned_db.close()
            self._owned_db = None
        self.service = service

    def close(self) -> None:
        if self._owned_db is not None:
            self._owned_db.close()
            self._owned_db = None

    def capabilities(self, _args: dict[str, Any]) -> str:
        integrations = sorted(set(self.capabilities_provider()))
        return _json({
            "types": {
                "website": "HTTP page with CSS/text/price extraction",
                "custom": "REST/JSON endpoint with JSONPath extraction",
                "instagram": "Meta Graph API monitor",
                "browser": "Authenticated Playwright page or DM snapshot",
                "tool": "Bounded workflow using existing Ares or connected MCP tools",
            },
            "presets": {
                "instagram_dm": {
                    "type": "browser",
                    "requires": "Connected Playwright MCP with an authenticated Instagram session",
                },
                "phone_notifications": {
                    "type": "tool",
                    "config": {"tool_name": "phone_get_notifications", "arguments": {"limit": 20}},
                },
            },
            "tool_monitors_enabled": self.tool_monitors_enabled,
            "built_in_observation_tools": [
                "phone_get_notifications", "phone_status", "fetch_url", "web_search",
                "read_file", "search_files", "search_memory", "search_actions",
                "list_tasks", "list_goals", "telephony_list_calls", "get_current_datetime",
            ],
            "connected_integration_tools": integrations,
            "safety": "Observation tools only by default; consequential steps require two explicit opt-ins.",
        })

    def create(self, args: dict[str, Any]) -> str:
        monitor_type = str(args.get("type") or "website").casefold()
        if monitor_type in {"tool", "browser"} and not self.tool_monitors_enabled:
            return "Error: Tool-backed watchers are disabled in Ares watcher configuration."
        config = dict(args.get("config") or {})
        url = args.get("url") or None
        preset_url, preset_config = _browser_preset(str(args.get("preset") or config.get("preset") or ""))
        if preset_config:
            monitor_type = "browser"
            url = url or preset_url
            config = {**preset_config, **config}
        if args.get("tool_name"):
            monitor_type = "tool"
            config.setdefault("tool_name", str(args["tool_name"]))
            config.setdefault("arguments", dict(args.get("arguments") or {}))
        validation_error = self._validation_error(monitor_type, str(url or ""), config)
        if validation_error:
            return f"Error: {validation_error}"
        try:
            goal_ids = self._goal_ids(args)
        except ValueError as exc:
            return f"Error: {exc}"
        monitor = Monitor(
            id=str(uuid4()),
            name=str(args.get("name") or ""),
            type=monitor_type,
            url=str(url) if url else None,
            config=config,
            interval_seconds=int(args.get("interval_seconds", 900)),
            ai_action=str(args.get("ai_action") or "notify"),
            ai_prompt=args.get("ai_prompt") or None,
            enabled=bool(args.get("enabled", True)),
        )
        self.db.insert_monitor(monitor)
        if goal_ids:
            try:
                for goal_id in goal_ids:
                    self.goal_store.link(goal_id, link_type="watcher", ref_id=monitor.id)
            except Exception as exc:
                self.goal_store.unlink_reference(link_type="watcher", ref_id=monitor.id)
                self.db.delete_monitor(monitor.id)
                return f"Error: Watcher creation was rolled back because goal linking failed: {exc}"
        return _json({
            "created": True,
            "watcher": monitor.public_dict(),
            "linked_goal_id": goal_ids[0] if len(goal_ids) == 1 else None,
            "linked_goal_ids": goal_ids,
            "next_step": "Use run_watcher_now to capture the baseline immediately.",
        })

    def list(self, args: dict[str, Any]) -> str:
        values = self.db.list_monitors(enabled_only=bool(args.get("enabled_only", False)))
        query = str(args.get("query") or "").casefold().strip()
        if query:
            values = [item for item in values if query in f"{item.name} {item.type} {item.url or ''}".casefold()]
        limit = max(1, min(int(args.get("limit", 100)), 500))
        return _json({"count": min(len(values), limit), "watchers": [item.public_dict() for item in values[:limit]]})

    def get(self, args: dict[str, Any]) -> str:
        monitor = self._monitor(str(args.get("watcher_id") or ""))
        if monitor is None:
            return "Error: Watcher not found."
        snapshot = self.db.get_latest_snapshot(monitor.id)
        return _json({
            "watcher": monitor.public_dict(),
            "linked_goals": [
                {"goal_id": item["goal_id"], "title": item["title"], "status": item["status"]}
                for item in (self.goal_store.linked_goals(link_type="watcher", ref_id=monitor.id) if self.goal_store is not None else [])
            ],
            "latest_snapshot": snapshot.to_dict() if snapshot else None,
            "events": [item.to_dict() for item in self.db.list_events(monitor.id, limit=30)],
            "checks": [item.to_dict() for item in self.db.list_check_runs(monitor.id, limit=50)],
        })

    def update(self, args: dict[str, Any]) -> str:
        monitor = self._monitor(str(args.get("watcher_id") or ""))
        if monitor is None:
            return "Error: Watcher not found."
        goal_ids: list[int] | None = None
        if "goal_ids" in args or "goal_id" in args:
            try:
                goal_ids = self._goal_ids(args)
            except ValueError as exc:
                return f"Error: {exc}"
        preset_url, preset_config = _browser_preset(str(args.get("preset") or ""))
        if preset_config:
            monitor.type = "browser"
            monitor.url = str(args.get("url") or monitor.url or preset_url or "") or None
            monitor.config = {**preset_config, **monitor.config}
        for key in ("name", "url", "interval_seconds", "ai_action", "ai_prompt", "enabled"):
            if key in args and args[key] is not None:
                setattr(monitor, key, args[key])
        if args.get("config") is not None:
            monitor.config = {**monitor.config, **dict(args["config"])}
        validation_error = self._validation_error(monitor.type, str(monitor.url or ""), monitor.config)
        if validation_error:
            return f"Error: {validation_error}"
        monitor.interval_seconds = int(monitor.interval_seconds)
        monitor.enabled = bool(monitor.enabled)
        monitor.__post_init__()
        self.db.update_monitor(monitor)
        linked_goals: list[dict[str, Any]] = []
        if goal_ids is not None:
            current = {
                int(item["goal_id"])
                for item in self.goal_store.linked_goals(link_type="watcher", ref_id=monitor.id)
            } if self.goal_store is not None else set()
            target = set(goal_ids)
            for goal_id in current - target:
                self.goal_store.unlink(goal_id, link_type="watcher", ref_id=monitor.id)
            for goal_id in target - current:
                self.goal_store.link(goal_id, link_type="watcher", ref_id=monitor.id)
            if self.goal_store is not None:
                linked_goals = [
                    {"goal_id": item["goal_id"], "title": item["title"], "status": item["status"]}
                    for item in self.goal_store.linked_goals(link_type="watcher", ref_id=monitor.id)
                ]
        return _json({"updated": True, "watcher": monitor.public_dict(), "linked_goals": linked_goals})

    def delete(self, args: dict[str, Any]) -> str:
        if not bool(args.get("confirm")):
            return "Confirmation required: deleting a watcher also deletes its snapshots, incidents, and run history."
        watcher_id = str(args.get("watcher_id") or "")
        deleted = self.db.delete_monitor(watcher_id)
        unlinked_goals = (
            self.goal_store.unlink_reference(link_type="watcher", ref_id=watcher_id)
            if deleted and self.goal_store is not None else []
        )
        return _json({"deleted": deleted, "watcher_id": watcher_id, "unlinked_goal_ids": unlinked_goals})

    def pause(self, args: dict[str, Any]) -> str:
        return self._set_enabled(args, False)

    def resume(self, args: dict[str, Any]) -> str:
        return self._set_enabled(args, True)

    async def run_now(self, args: dict[str, Any]) -> str:
        monitor = self._monitor(str(args.get("watcher_id") or ""))
        if monitor is None:
            return "Error: Watcher not found."
        if self.service is None:
            return "Error: The Ares watcher runtime is not active; start Ares with --all."
        event = await self.service.scheduler.check_monitor(monitor, force=True)
        refreshed = self.db.get_monitor(monitor.id)
        return _json({
            "checked": True,
            "watcher": refreshed.public_dict() if refreshed else monitor.public_dict(),
            "change_detected": event is not None,
            "event": event.to_dict() if event else None,
        })

    def events(self, args: dict[str, Any]) -> str:
        values = self.db.list_events(
            str(args.get("watcher_id") or "") or None,
            limit=max(1, min(int(args.get("limit", 100)), 500)),
            severity=str(args.get("severity") or "") or None,
            unacknowledged=bool(args.get("unacknowledged_only", False)),
        )
        return _json({"count": len(values), "events": [item.to_dict() for item in values]})

    def acknowledge(self, args: dict[str, Any]) -> str:
        event_id = str(args.get("event_id") or "")
        return _json({"acknowledged": self.db.acknowledge_event(event_id), "event_id": event_id})

    def overview(self, _args: dict[str, Any]) -> str:
        return _json(self.db.overview())

    def _set_enabled(self, args: dict[str, Any], enabled: bool) -> str:
        monitor = self._monitor(str(args.get("watcher_id") or ""))
        if monitor is None:
            return "Error: Watcher not found."
        monitor.enabled = enabled
        if enabled:
            monitor.error_count = 0
            monitor.next_check_at = None
        self.db.update_monitor(monitor)
        return _json({"updated": True, "watcher": monitor.public_dict()})

    def _monitor(self, watcher_id: str) -> Monitor | None:
        return self.db.get_monitor(watcher_id)

    def _goal_ids(self, args: dict[str, Any]) -> list[int]:
        raw = args.get("goal_ids")
        values = raw if isinstance(raw, list) else ([] if raw is None else [raw])
        if args.get("goal_id") is not None:
            values = [*values, args["goal_id"]]
        try:
            goal_ids = list(dict.fromkeys(int(value) for value in values))
        except (TypeError, ValueError) as exc:
            raise ValueError("goal_ids must contain integer goal IDs") from exc
        if len(goal_ids) > 50:
            raise ValueError("A watcher can be linked to at most 50 goals")
        if any(goal_id <= 0 for goal_id in goal_ids):
            raise ValueError("goal IDs must be positive integers")
        if goal_ids and self.goal_store is None:
            raise ValueError("Goal storage is unavailable in this Ares runtime")
        for goal_id in goal_ids:
            if self.goal_store.get(goal_id) is None:
                raise ValueError(f"Goal #{goal_id} was not found")
        return goal_ids

    def _validation_error(self, monitor_type: str, url: str, config: dict[str, Any]) -> str | None:
        if monitor_type == "website" and not url:
            return "Website watchers require a target URL."
        if monitor_type == "custom" and not (url or config.get("api_url")):
            return "API watchers require a target URL or config.api_url."
        if monitor_type == "instagram" and not (url or config.get("api_url")):
            return "Instagram Graph watchers require a target URL or config.api_url."
        if monitor_type not in {"tool", "browser"}:
            return None
        if not self.tool_monitors_enabled:
            return "Tool-backed watchers are disabled."
        steps = config.get("steps")
        if monitor_type == "browser" and not steps:
            steps = browser_steps(url, config)
        elif monitor_type == "tool" and not steps and config.get("tool_name"):
            steps = [{"tool_name": config["tool_name"], "arguments": config.get("arguments") or {}}]
        if not isinstance(steps, list) or not steps:
            return "Tool watchers require config.steps or tool_name."
        allow_mutating = self.allow_mutating_tool_steps and bool(config.get("allow_mutating_tools"))
        try:
            for step in steps:
                if not isinstance(step, dict):
                    return "Each workflow step must be an object."
                tool_name = str(step.get("tool_name") or "")
                allow_navigation = bool(step.get("allow_navigation")) or (
                    monitor_type == "browser" and tool_name.casefold().endswith("browser_navigate")
                )
                validate_tool_step(tool_name, allow_navigation=allow_navigation, allow_mutating=allow_mutating)
        except FetcherError as exc:
            return str(exc)
        return None


__all__ = ["WatcherToolHandlers"]
