"""FastAPI control plane and real-time dashboard for Ares Watcher."""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from ares.watcher.models import Monitor, redact_secrets, utc_now
from ares.watcher.service import WatcherService


STATIC_DIR = Path(__file__).parent / "static"


class MonitorPayload(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    type: str = Field(pattern="^(website|custom|instagram|browser|tool)$")
    url: str | None = None
    interval_seconds: int = Field(default=900, ge=20, le=31_536_000)
    ai_action: str = Field(default="notify", pattern="^(notify|suggest|auto)$")
    ai_prompt: str | None = Field(default=None, max_length=4000)
    enabled: bool = True
    config: dict[str, Any] = Field(default_factory=dict)
    goal_ids: list[int] = Field(default_factory=list)


class MonitorPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    url: str | None = None
    interval_seconds: int | None = Field(default=None, ge=20, le=31_536_000)
    ai_action: str | None = Field(default=None, pattern="^(notify|suggest|auto)$")
    ai_prompt: str | None = Field(default=None, max_length=4000)
    enabled: bool | None = None
    config: dict[str, Any] | None = None
    goal_ids: list[int] | None = None


class SettingsPatch(BaseModel):
    notifications: dict[str, dict[str, Any]]


def create_app(*, service: WatcherService | None = None, database_path: str | Path | None = None,
               notification_settings: dict[str, Any] | None = None, start_scheduler: bool = True,
               api_token: str | None = None, stop_service_on_shutdown: bool | None = None,
               settings_saver: Callable[[dict[str, Any]], None] | None = None,
               goal_store: Any | None = None, close_goal_store_on_shutdown: bool = False) -> FastAPI:
    owned = service is None
    should_stop = owned if stop_service_on_shutdown is None else stop_service_on_shutdown
    watcher = service or WatcherService(database_path or Path("~/.ares/data/watchers.db").expanduser(), notification_settings=notification_settings)
    token = api_token or os.environ.get("ARES_WATCHER_API_TOKEN", "")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if start_scheduler:
            await watcher.start()
        yield
        if should_stop:
            await watcher.stop()
        if close_goal_store_on_shutdown and goal_store is not None:
            goal_store.close()

    app = FastAPI(title="Ares Watcher Control Plane", version="1.0.0", lifespan=lifespan)
    app.state.watcher = watcher
    app.state.goal_store = goal_store

    def goal_summary(goal: dict[str, Any]) -> dict[str, Any]:
        return {
            key: goal.get(key)
            for key in (
                "goal_id", "title", "status", "priority", "progress_percent",
                "target_date", "is_overdue", "days_remaining",
            )
        }

    def validate_goal_ids(values: list[int] | None) -> list[int]:
        ids = list(dict.fromkeys(int(value) for value in (values or [])))
        if len(ids) > 50:
            raise HTTPException(422, "A watcher can be linked to at most 50 goals")
        if any(value <= 0 for value in ids):
            raise HTTPException(422, "Goal IDs must be positive integers")
        if ids and goal_store is None:
            raise HTTPException(503, "Goal storage is unavailable in this runtime")
        missing = [value for value in ids if goal_store.get(value) is None]
        if missing:
            raise HTTPException(404, f"Goal #{missing[0]} was not found")
        return ids

    def set_goal_links(monitor_id: str, goal_ids: list[int]) -> None:
        if goal_store is None:
            return
        current = {
            int(goal["goal_id"])
            for goal in goal_store.linked_goals(link_type="watcher", ref_id=monitor_id)
        }
        target = set(goal_ids)
        for goal_id in current - target:
            goal_store.unlink(goal_id, link_type="watcher", ref_id=monitor_id)
        for goal_id in target - current:
            goal_store.link(goal_id, link_type="watcher", ref_id=monitor_id)

    def public_monitor(monitor: Monitor) -> dict[str, Any]:
        payload = monitor.public_dict()
        if goal_store is None:
            return {**payload, "linked_goals": [], "goal_signal_count": 0, "open_goal_signals": []}
        linked = goal_store.linked_goals(link_type="watcher", ref_id=monitor.id)
        signals = goal_store.list_watcher_signals(
            watcher_id=monitor.id, include_acknowledged=True, limit=500,
        )
        open_signals = [signal for signal in signals if not signal.get("acknowledged")]
        return {
            **payload,
            "linked_goals": [goal_summary(goal) for goal in linked],
            "goal_signal_count": len(signals),
            "open_goal_signals": open_signals,
        }

    def public_event(event: Any) -> dict[str, Any]:
        payload = event.to_dict()
        signals = [] if goal_store is None else goal_store.list_watcher_signals(
            source_event_id=event.id, include_acknowledged=True, limit=100,
        )
        for signal in signals:
            goal = goal_store.get(int(signal["goal_id"]))
            signal["goal_title"] = goal["title"] if goal else f"Goal #{signal['goal_id']}"
            signal["goal_status"] = goal["status"] if goal else "unknown"
        return {**payload, "goal_signals": signals}

    def public_overview() -> dict[str, Any]:
        payload = watcher.db.overview()
        monitors = watcher.db.list_monitors()
        decorated = [public_monitor(monitor) for monitor in monitors]
        return {
            **payload,
            "goal_linked_watchers": sum(bool(item["linked_goals"]) for item in decorated),
            "linked_goals": len({
                int(goal["goal_id"])
                for monitor in decorated
                for goal in monitor["linked_goals"]
            }),
            "open_goal_signals": sum(len(item["open_goal_signals"]) for item in decorated),
        }

    @app.middleware("http")
    async def local_auth(request: Request, call_next):
        if token and request.url.path.startswith("/api/") and request.headers.get("X-Ares-Token") != token:
            from fastapi.responses import JSONResponse
            return JSONResponse({"detail": "Invalid watcher API token"}, status_code=401)
        return await call_next(request)

    @app.get("/", include_in_schema=False)
    async def index():
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/health")
    async def health():
        return {"status": "operational", "scheduler_running": watcher.scheduler.running, "database": watcher.db.db_path,
                "subscribers": len(watcher._subscribers), "time": utc_now().isoformat()}

    @app.get("/api/overview")
    async def overview():
        return public_overview()

    @app.get("/api/goals")
    async def goals():
        if goal_store is None:
            return []
        return [
            {**goal_summary(goal), "watcher_ids": goal_store.linked_refs(int(goal["goal_id"]))["watchers"]}
            for goal in goal_store.list_all(limit=500)
        ]

    @app.get("/api/capabilities")
    async def capabilities():
        return {
            "monitor_types": sorted(watcher.scheduler.fetchers),
            "tool_workflows": "tool" in watcher.scheduler.fetchers,
            "authenticated_browser": "browser" in watcher.scheduler.fetchers,
            "safety": "Observation-only unless global and per-watcher mutation flags are both enabled.",
        }

    @app.get("/api/monitors")
    async def monitors(enabled: bool | None = None):
        values = watcher.db.list_monitors(enabled_only=enabled is True)
        if enabled is False:
            values = [item for item in values if not item.enabled]
        return [public_monitor(item) for item in values]

    @app.post("/api/monitors", status_code=201)
    async def create_monitor(payload: MonitorPayload):
        values = payload.model_dump()
        goal_ids = validate_goal_ids(values.pop("goal_ids", []))
        monitor = Monitor(id=str(uuid4()), **values)
        watcher.db.insert_monitor(monitor)
        try:
            set_goal_links(monitor.id, goal_ids)
        except Exception as exc:
            if goal_store is not None:
                goal_store.unlink_reference(link_type="watcher", ref_id=monitor.id)
            watcher.db.delete_monitor(monitor.id)
            raise HTTPException(409, f"Watcher creation was rolled back because goal linking failed: {exc}") from exc
        result = public_monitor(monitor)
        await watcher.publish("monitor.created", {"monitor": result})
        return result

    @app.get("/api/monitors/{monitor_id}")
    async def monitor_detail(monitor_id: str):
        monitor = _monitor_or_404(watcher, monitor_id)
        snapshot = watcher.db.get_latest_snapshot(monitor_id)
        return {"monitor": public_monitor(monitor), "latest_snapshot": snapshot.to_dict() if snapshot else None,
                "events": [public_event(item) for item in watcher.db.list_events(monitor_id, limit=30)],
                "checks": [item.to_dict() for item in watcher.db.list_check_runs(monitor_id, limit=100)]}

    @app.patch("/api/monitors/{monitor_id}")
    async def update_monitor(monitor_id: str, payload: MonitorPatch):
        monitor = _monitor_or_404(watcher, monitor_id)
        values = payload.model_dump(exclude_unset=True)
        goal_ids = validate_goal_ids(values.pop("goal_ids")) if "goal_ids" in values else None
        for key, value in values.items():
            setattr(monitor, key, _merge_redacted(monitor.config, value) if key == "config" else value)
        monitor.__post_init__()
        watcher.db.update_monitor(monitor)
        if goal_ids is not None:
            set_goal_links(monitor.id, goal_ids)
        result = public_monitor(monitor)
        await watcher.publish("monitor.updated", {"monitor": result})
        return result

    @app.delete("/api/monitors/{monitor_id}", status_code=204)
    async def delete_monitor(monitor_id: str):
        if not watcher.db.delete_monitor(monitor_id):
            raise HTTPException(404, "Monitor not found")
        if goal_store is not None:
            goal_store.unlink_reference(link_type="watcher", ref_id=monitor_id)
        await watcher.publish("monitor.deleted", {"monitor_id": monitor_id})

    @app.post("/api/monitors/{monitor_id}/pause")
    async def pause_monitor(monitor_id: str):
        return await _set_enabled(watcher, monitor_id, False)

    @app.post("/api/monitors/{monitor_id}/resume")
    async def resume_monitor(monitor_id: str):
        return await _set_enabled(watcher, monitor_id, True)

    @app.post("/api/monitors/{monitor_id}/check", status_code=202)
    async def check_monitor(monitor_id: str):
        monitor = _monitor_or_404(watcher, monitor_id)
        asyncio.create_task(watcher.scheduler.check_monitor(monitor, force=True), name=f"watcher-manual-{monitor_id}")
        return {"accepted": True, "monitor_id": monitor_id}

    @app.get("/api/events")
    async def events(monitor_id: str | None = None, severity: str | None = None, unacknowledged: bool = False,
                     limit: int = Query(100, ge=1, le=500)):
        return [public_event(item) for item in watcher.db.list_events(monitor_id, limit=limit, severity=severity, unacknowledged=unacknowledged)]

    @app.post("/api/events/{event_id}/acknowledge")
    async def acknowledge(event_id: str):
        if not watcher.db.acknowledge_event(event_id):
            raise HTTPException(404, "Event not found")
        await watcher.publish("alert.acknowledged", {"event_id": event_id})
        return {"acknowledged": True, "event_id": event_id}

    @app.get("/api/checks")
    async def checks(monitor_id: str | None = None, limit: int = Query(200, ge=1, le=1000)):
        return [item.to_dict() for item in watcher.db.list_check_runs(monitor_id, limit)]

    @app.get("/api/notifications")
    async def notifications(event_id: str | None = None, limit: int = Query(100, ge=1, le=500)):
        return [item.to_dict() for item in watcher.db.list_notifications(event_id=event_id, limit=limit)]

    @app.get("/api/settings")
    async def settings():
        return {"notifications": redact_secrets(watcher.notifier.settings), "security": {
            "api_token_enabled": bool(token), "local_only_recommended": True,
        }, "service": {
            "poll_seconds": watcher.poll_seconds,
            "max_concurrency": watcher.scheduler.max_concurrency,
            "monitor_types": sorted(watcher.scheduler.fetchers),
        }}

    @app.patch("/api/settings")
    async def update_settings(payload: SettingsPatch):
        merged = _merge_redacted(watcher.notifier.settings, payload.notifications)
        watcher.notifier.settings = merged
        if settings_saver:
            settings_saver(merged)
        public = redact_secrets(merged)
        await watcher.publish("settings.updated", {"notifications": public})
        return {"notifications": public}

    @app.websocket("/ws")
    async def realtime(websocket: WebSocket):
        if token and websocket.query_params.get("token") != token:
            await websocket.close(code=4401)
            return
        await websocket.accept()
        await websocket.send_json({"type": "connected", "payload": {"overview": public_overview()}})
        subscription = watcher.subscribe()
        pending = asyncio.create_task(anext(subscription))
        try:
            while True:
                done, _ = await asyncio.wait({pending}, timeout=25)
                if done:
                    message = pending.result()
                    pending = asyncio.create_task(anext(subscription))
                else:
                    message = {"type":"heartbeat","payload":{"time":utc_now().isoformat()}}
                await websocket.send_json(message)
        except (WebSocketDisconnect, RuntimeError, StopAsyncIteration):
            pass
        finally:
            pending.cancel()
            with suppress(asyncio.CancelledError, StopAsyncIteration):
                await pending
            await subscription.aclose()

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="watcher-static")
    return app


def _monitor_or_404(service: WatcherService, monitor_id: str) -> Monitor:
    monitor = service.db.get_monitor(monitor_id)
    if not monitor:
        raise HTTPException(404, "Monitor not found")
    return monitor


async def _set_enabled(service: WatcherService, monitor_id: str, enabled: bool) -> dict[str, Any]:
    monitor = _monitor_or_404(service, monitor_id)
    monitor.enabled = enabled
    monitor.error_count = 0 if enabled else monitor.error_count
    monitor.next_check_at = None if enabled else monitor.next_check_at
    service.db.update_monitor(monitor)
    await service.publish("monitor.updated", {"monitor": monitor.public_dict()})
    return monitor.public_dict()


def _merge_redacted(existing: Any, incoming: Any) -> Any:
    """Preserve stored secrets when a dashboard submits its redacted view."""
    if incoming == "***REDACTED***":
        return existing
    if isinstance(existing, dict) and isinstance(incoming, dict):
        merged = dict(existing)
        merged.update({key: _merge_redacted(existing.get(key), value) for key, value in incoming.items()})
        return merged
    return incoming


def run_dashboard(host: str = "127.0.0.1", port: int = 8080, database_path: str | Path | None = None,
                  notification_settings: dict[str, Any] | None = None, max_concurrency: int = 8,
                  poll_seconds: float = 5.0) -> None:
    import uvicorn
    from ares.skills.goals import GoalStore
    service = WatcherService(database_path or Path("~/.ares/data/watchers.db").expanduser(),
                             notification_settings=notification_settings, max_concurrency=max_concurrency, poll_seconds=poll_seconds)
    goal_store = GoalStore()
    def persist_settings(settings: dict[str, Any]) -> None:
        from ares.config import load_config, save_config
        config = load_config(); config.watcher.notifications = settings; save_config(config)
    uvicorn.run(create_app(
        service=service,
        stop_service_on_shutdown=True,
        settings_saver=persist_settings,
        goal_store=goal_store,
        close_goal_store_on_shutdown=True,
    ), host=host, port=port, log_level="info")
