import json

import pytest

from ares.goals import GoalStore
from ares.watcher.service import WatcherService
from ares.watcher.tools import WatcherToolHandlers


def test_watcher_tool_handlers_manage_browser_and_tool_watchers(tmp_path):
    handlers = WatcherToolHandlers(
        tmp_path / "tools.db",
        capabilities_provider=lambda: ["mcp__playwright__browser_snapshot"],
    )
    try:
        created = json.loads(handlers.create({
            "name": "Instagram DMs",
            "preset": "instagram_dm",
            "interval_seconds": 60,
        }))["watcher"]
        assert created["type"] == "browser"
        assert created["url"].endswith("/direct/inbox/")
        assert json.loads(handlers.list({}))["count"] == 1
        assert json.loads(handlers.pause({"watcher_id": created["id"]}))["watcher"]["enabled"] is False
        assert json.loads(handlers.resume({"watcher_id": created["id"]}))["watcher"]["enabled"] is True
        capabilities = json.loads(handlers.capabilities({}))
        assert "browser" in capabilities["types"]
        assert "mcp__playwright__browser_snapshot" in capabilities["connected_integration_tools"]
        assert "Confirmation required" in handlers.delete({"watcher_id": created["id"]})
        assert json.loads(handlers.delete({"watcher_id": created["id"], "confirm": True}))["deleted"] is True
    finally:
        handlers.close()

@pytest.mark.asyncio
async def test_watcher_run_now_uses_shared_ares_tool_runtime(tmp_path):
    calls = []

    async def runner(name, arguments):
        calls.append((name, arguments))
        return json.dumps({"status": "ready"})

    service = WatcherService(tmp_path / "runtime.db", tool_runner=runner, notification_settings={})
    handlers = WatcherToolHandlers(tmp_path / "runtime.db", service=service)
    try:
        watcher = json.loads(handlers.create({
            "name": "Ares status",
            "type": "tool",
            "tool_name": "get_current_datetime",
            "interval_seconds": 60,
        }))["watcher"]
        result = json.loads(await handlers.run_now({"watcher_id": watcher["id"]}))
        assert result["checked"] is True
        assert result["watcher"]["last_status"] == "ok"
        assert calls == [("get_current_datetime", {})]
    finally:
        await service.stop()


@pytest.mark.asyncio
async def test_run_now_requires_active_runtime(tmp_path):
    handlers = WatcherToolHandlers(tmp_path / "offline.db")
    try:
        watcher = json.loads(handlers.create({"name": "Offline", "type": "website", "url": "https://example.com"}))["watcher"]
        assert (await handlers.run_now({"watcher_id": watcher["id"]})).startswith("Error:")
    finally:
        handlers.close()


def test_create_watcher_can_link_goal_and_delete_cleans_reference(tmp_path):
    goals = GoalStore(tmp_path / "ares.db")
    goal = goals.create("Buy a laptop under budget")
    handlers = WatcherToolHandlers(tmp_path / "watchers.db", goal_store=goals)
    try:
        created = json.loads(handlers.create({
            "name": "Laptop price",
            "type": "website",
            "url": "https://example.com/laptop",
            "goal_id": goal["goal_id"],
        }))
        watcher_id = created["watcher"]["id"]
        assert created["linked_goal_id"] == goal["goal_id"]
        assert goals.linked_refs(goal["goal_id"])["watchers"] == [watcher_id]
        assert json.loads(handlers.get({"watcher_id": watcher_id}))["linked_goals"][0]["goal_id"] == goal["goal_id"]

        deleted = json.loads(handlers.delete({"watcher_id": watcher_id, "confirm": True}))
        assert deleted["unlinked_goal_ids"] == [goal["goal_id"]]
        assert goals.linked_refs(goal["goal_id"])["watchers"] == []
    finally:
        handlers.close()
        goals.close()
