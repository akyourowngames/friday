"""First-class Ares tool handlers for creating and operating watchers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable
from uuid import uuid4

from ares.tools.results import error_result, structured_result, wants_structured
from ares.watcher.database import WatcherDatabase
from ares.watcher.fetchers.base import FetcherError
from ares.watcher.fetchers.tool import browser_steps, validate_tool_step
from ares.watcher.models import Monitor
from ares.watcher.upgrades import (
    WatcherPolicyError,
    health_projection,
    normalize_watcher_policy,
    project_watcher_event,
)

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


# These fields are opt-in additions.  Keeping them in monitor.config means the
# existing SQLite monitor representation stays backwards-compatible while a
# deployed runtime can begin using policy-aware watchers immediately.
_UPGRADE_CONFIG_FIELDS = {
    "condition_policy", "conditions", "condition", "condition_operator", "operator",
    "alert_conditions", "alert_policy", "alerts", "baseline", "workflow", "priority",
    "expires_at", "sensitive_content",
}


def _advanced_requested(args: dict[str, Any], config: dict[str, Any] | None = None) -> bool:
    """Return whether the caller selected an upgraded watcher surface."""
    try:
        if wants_structured(args):
            return True
    except ValueError:
        # Validation is reported by the handler so legacy callers never see an
        # import-time/tool-dispatch exception.
        return True
    return any(key in args for key in _UPGRADE_CONFIG_FIELDS) or bool(
        set((config or {}).keys()) & _UPGRADE_CONFIG_FIELDS
    )


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
        config = self._upgrade_config(args, dict(args.get("config") or {}))
        structured = self._structured_requested(args)
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
            return error_result(validation_error, code="validation") if structured else f"Error: {validation_error}"
        try:
            goal_ids = self._goal_ids(args)
        except ValueError as exc:
            return error_result(str(exc), code="validation") if structured else f"Error: {exc}"
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
        payload = {
            "created": True,
            "watcher": monitor.public_dict(),
            "linked_goal_id": goal_ids[0] if len(goal_ids) == 1 else None,
            "linked_goal_ids": goal_ids,
            "next_step": "Use run_watcher_now to capture the baseline immediately.",
        }
        if structured:
            return structured_result(
                f"Created watcher '{monitor.name}'.",
                data=payload,
                next_actions=[{"tool": "run_watcher_now", "arguments": {"watcher_id": monitor.id}}],
                provenance={"watcher_id": monitor.id, "policy": self._policy_summary(monitor)},
            )
        return _json(payload)

    def list(self, args: dict[str, Any]) -> str:
        values = self.db.list_monitors(enabled_only=bool(args.get("enabled_only", False)))
        query = str(args.get("query") or "").casefold().strip()
        if query:
            values = [item for item in values if query in f"{item.name} {item.type} {item.url or ''}".casefold()]
        limit = max(1, min(int(args.get("limit", 100)), 500))
        values = values[:limit]
        if not self._structured_requested(args) and not _advanced_requested(args):
            return _json({"count": len(values), "watchers": [item.public_dict() for item in values]})
        watchers = [self._watcher_projection(item, include_values=bool(args.get("include_values", False))) for item in values]
        payload = {"count": len(watchers), "watchers": watchers}
        if self._structured_requested(args):
            return structured_result(
                f"Found {len(watchers)} watcher(s).",
                data=payload,
                metrics={"watcher_count": len(watchers)},
            )
        return _json(payload)

    def get(self, args: dict[str, Any]) -> str:
        monitor = self._monitor(str(args.get("watcher_id") or ""))
        if monitor is None:
            return error_result("Watcher not found.", code="not_found", status="not_found") if self._structured_requested(args) else "Error: Watcher not found."
        if self._structured_requested(args) or _advanced_requested(args, monitor.config):
            projection = self._watcher_projection(monitor, include_values=bool(args.get("include_values", False)))
            projection["linked_goals"] = [
                {"goal_id": item["goal_id"], "title": item["title"], "status": item["status"]}
                for item in (self.goal_store.linked_goals(link_type="watcher", ref_id=monitor.id) if self.goal_store is not None else [])
            ]
            if self._structured_requested(args):
                return structured_result(
                    f"Watcher '{monitor.name}' inspection is ready.",
                    data=projection,
                    provenance={"watcher_id": monitor.id, "policy": self._policy_summary(monitor)},
                    metrics={"event_count": len(projection["events"]), "check_count": len(projection["checks"])},
                )
            return _json(projection)
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
            return error_result("Watcher not found.", code="not_found", status="not_found") if self._structured_requested(args) else "Error: Watcher not found."
        structured = self._structured_requested(args)
        goal_ids: list[int] | None = None
        if "goal_ids" in args or "goal_id" in args:
            try:
                goal_ids = self._goal_ids(args)
            except ValueError as exc:
                return error_result(str(exc), code="validation") if structured else f"Error: {exc}"
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
        monitor.config = self._upgrade_config(args, monitor.config)
        validation_error = self._validation_error(monitor.type, str(monitor.url or ""), monitor.config)
        if validation_error:
            return error_result(validation_error, code="validation") if structured else f"Error: {validation_error}"
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
        payload = {"updated": True, "watcher": monitor.public_dict(), "linked_goals": linked_goals}
        if structured:
            return structured_result(
                f"Updated watcher '{monitor.name}'.", data=payload,
                provenance={"watcher_id": monitor.id, "policy": self._policy_summary(monitor)},
            )
        return _json(payload)

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
            return error_result("Watcher not found.", code="not_found", status="not_found") if self._structured_requested(args) else "Error: Watcher not found."
        if self.service is None:
            message = "The Ares watcher runtime is not active; start Ares with --all."
            return error_result(message, code="runtime_unavailable") if self._structured_requested(args) else f"Error: {message}"
        event = await self.service.scheduler.check_monitor(monitor, force=True)
        refreshed = self.db.get_monitor(monitor.id)
        payload = {
            "checked": True,
            "watcher": refreshed.public_dict() if refreshed else monitor.public_dict(),
            "change_detected": event is not None,
            "event": event.to_dict() if event else None,
        }
        if self._structured_requested(args):
            if event is not None:
                payload["event"] = self._event_projection(event, monitor, include_values=bool(args.get("include_values", False)))
            return structured_result(
                "Watcher check completed.", data=payload,
                provenance={"watcher_id": monitor.id}, metrics={"change_detected": bool(event)},
            )
        return _json(payload)

    def events(self, args: dict[str, Any]) -> str:
        values = self.db.list_events(
            str(args.get("watcher_id") or "") or None,
            limit=max(1, min(int(args.get("limit", 100)), 500)),
            severity=str(args.get("severity") or "") or None,
            unacknowledged=bool(args.get("unacknowledged_only", False)),
        )
        include_suppressed = bool(args.get("include_suppressed", True))
        feedback = str(args.get("feedback") or "").strip().casefold()
        if not include_suppressed:
            values = [item for item in values if not item.suppressed]
        if feedback:
            values = [item for item in values if str(item.feedback or "").casefold() == feedback]
        if not self._structured_requested(args) and not _advanced_requested(args):
            return _json({"count": len(values), "events": [item.to_dict() for item in values]})
        monitor_lookup = {item.id: item for item in self.db.list_monitors()}
        events = [
            self._event_projection(item, monitor_lookup.get(item.monitor_id), include_values=bool(args.get("include_values", False)))
            for item in values
        ]
        payload = {"count": len(events), "events": events}
        if self._structured_requested(args):
            return structured_result(
                f"Found {len(events)} watcher event(s).", data=payload,
                metrics={"event_count": len(events), "suppressed": sum(1 for item in values if item.suppressed)},
            )
        return _json(payload)

    def acknowledge(self, args: dict[str, Any]) -> str:
        event_id = str(args.get("event_id") or "")
        feedback = str(args.get("feedback") or "").strip().casefold() or None
        if feedback not in {None, "reviewed", "valid", "false_positive", "not_sure"}:
            message = "feedback must be reviewed, valid, false_positive, or not_sure"
            return error_result(message, code="validation") if self._structured_requested(args) else f"Error: {message}"
        note = str(args.get("feedback_note") or "").strip() or None
        if note and len(note) > 2_000:
            message = "feedback_note must be at most 2000 characters"
            return error_result(message, code="validation") if self._structured_requested(args) else f"Error: {message}"
        acknowledged = self.db.acknowledge_event(event_id, feedback=feedback, feedback_note=note)
        payload = {"acknowledged": acknowledged, "event_id": event_id, "feedback": feedback}
        if self._structured_requested(args):
            return structured_result(
                "Watcher event acknowledged." if acknowledged else "Watcher event was not found.",
                ok=acknowledged, status="completed" if acknowledged else "not_found", data=payload,
                errors=[] if acknowledged else [{"code": "not_found", "message": "Watcher event was not found."}],
            )
        return _json(payload)

    def overview(self, args: dict[str, Any]) -> str:
        payload = self.db.overview()
        if not self._structured_requested(args) and not _advanced_requested(args):
            return _json(payload)
        monitors = self.db.list_monitors()
        health = [self._watcher_projection(monitor, include_values=False)["health"] for monitor in monitors]
        payload["health"] = health
        payload["degraded_watchers"] = sum(item["status"] in {"degraded", "failed", "disabled"} for item in health)
        if self._structured_requested(args):
            return structured_result(
                "Watcher fleet overview is ready.", data=payload,
                metrics={"watcher_count": len(monitors), "degraded_watchers": payload["degraded_watchers"]},
            )
        return _json(payload)

    @staticmethod
    def _structured_requested(args: dict[str, Any]) -> bool:
        try:
            return wants_structured(args)
        except ValueError:
            return False

    @staticmethod
    def _upgrade_config(args: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
        """Fold optional upgraded tool arguments into durable monitor config."""
        result = dict(config)
        for key in _UPGRADE_CONFIG_FIELDS - {"expires_at"}:
            if key in args and args[key] is not None:
                result[key] = args[key]
        if args.get("expires_at") is not None:
            alerts = dict(result.get("alert_policy") or result.get("alerts") or {})
            alerts["expires_at"] = args["expires_at"]
            result["alert_policy"] = alerts
        return result

    def _policy_summary(self, monitor: Monitor) -> dict[str, Any]:
        """Expose policy shape without leaking fetched content or secrets."""
        try:
            policy = normalize_watcher_policy(monitor.config)
        except WatcherPolicyError:
            return {"configured": False, "error": "Invalid watcher policy"}
        alerts = policy["alert_policy"]
        return {
            "configured": _advanced_requested({}, monitor.config),
            "condition_operator": policy["operator"],
            "condition_count": len(policy["conditions"]),
            "priority": str(monitor.config.get("priority") or "normal"),
            "workflow_configured": bool(monitor.config.get("workflow") or monitor.config.get("steps")),
            "alert_policy": {
                "cooldown_seconds": alerts["cooldown_seconds"],
                "dedupe_window_seconds": alerts["dedupe_window_seconds"],
                "quiet_hours": alerts["quiet_hours"],
                "expires_at": alerts["expires_at"],
                "min_severity": alerts["min_severity"],
            },
        }

    @staticmethod
    def _safe_value(value: Any, *, sensitive: bool, include_values: bool) -> Any:
        if not sensitive or include_values:
            return value
        text = str(value or "")
        return {"redacted": True, "characters": len(text)}

    def _event_projection(self, event: Any, monitor: Monitor | None, *, include_values: bool) -> dict[str, Any]:
        source = event.to_dict() if hasattr(event, "to_dict") else dict(event)
        sensitive = bool(monitor and monitor.config.get("sensitive_content"))
        projection = project_watcher_event(source)
        if sensitive and not include_values:
            projection["summary"] = "Sensitive watcher change detected."
        projection.update({
            "change": {
                "previous": self._safe_value(source.get("old_value"), sensitive=sensitive, include_values=include_values),
                "current": self._safe_value(source.get("new_value"), sensitive=sensitive, include_values=include_values),
                "percent": source.get("change_percent"),
            },
            "confidence": source.get("confidence", 1.0),
            "suppressed": bool(source.get("suppressed", False)),
            "suppression_reason": source.get("suppression_reason"),
            "acknowledged": bool(source.get("acknowledged", False)),
            "feedback": source.get("feedback"),
            "feedback_note": source.get("feedback_note"),
        })
        return projection

    def _snapshot_projection(self, snapshot: Any, monitor: Monitor, *, include_values: bool) -> dict[str, Any] | None:
        if snapshot is None:
            return None
        source = snapshot.to_dict()
        sensitive = bool(monitor.config.get("sensitive_content"))
        return {
            "id": source["id"],
            "content_hash": source.get("content_hash"),
            "content": self._safe_value(source.get("content"), sensitive=sensitive, include_values=include_values),
            "price_value": source.get("price_value"),
            "metadata": source.get("metadata", {}),
            "created_at": source.get("created_at"),
        }

    def _watcher_projection(self, monitor: Monitor, *, include_values: bool) -> dict[str, Any]:
        snapshot = self.db.get_latest_snapshot(monitor.id)
        events = self.db.list_events(monitor.id, limit=50)
        checks = self.db.list_check_runs(monitor.id, limit=100)
        return {
            "watcher": monitor.public_dict(),
            "policy": self._policy_summary(monitor),
            "latest_snapshot": self._snapshot_projection(snapshot, monitor, include_values=include_values),
            "events": [self._event_projection(item, monitor, include_values=include_values) for item in events],
            "checks": [item.to_dict() for item in checks],
            "health": health_projection(
                monitor.public_dict(), [item.to_dict() for item in checks], [item.to_dict() for item in events],
                stale_after_seconds=max(60, int(monitor.config.get("stale_after_seconds", monitor.interval_seconds * 2))),
            ),
        }

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
        if _advanced_requested({}, config):
            try:
                normalize_watcher_policy(config)
            except WatcherPolicyError as exc:
                return str(exc)
            workflow = config.get("workflow")
            if workflow is not None:
                if not isinstance(workflow, dict):
                    return "workflow must be an object with bounded read-only steps"
                steps = workflow.get("steps", [])
                if not isinstance(steps, list) or len(steps) > 8:
                    return "workflow.steps must be a list containing at most 8 steps"
                try:
                    for step in steps:
                        if not isinstance(step, dict):
                            return "Each workflow step must be an object."
                        validate_tool_step(str(step.get("tool_name") or ""), allow_navigation=bool(step.get("allow_navigation")))
                except FetcherError as exc:
                    return str(exc)
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
