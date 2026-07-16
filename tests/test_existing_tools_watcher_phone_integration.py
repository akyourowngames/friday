"""Focused integration coverage for upgraded watcher and phone tool surfaces."""

from __future__ import annotations

import json

import pytest

from ares.models import AppConfig, PhoneConfig
from ares.tools.executor import ToolExecutor
from ares.watcher.fetchers.base import BaseFetcher, FetchResult
from ares.watcher.models import Event, Monitor
from ares.watcher.database import WatcherDatabase
from ares.watcher.scheduler import WatcherScheduler
from ares.watcher.tools import WatcherToolHandlers


class _SequenceFetcher(BaseFetcher):
    def __init__(self, values):
        self.values = list(values)

    async def fetch(self, _target, _config=None):
        return self.values.pop(0)

    async def close(self):
        return None


class _Store:
    def store(self, *_args, **_kwargs):
        return 1


@pytest.mark.asyncio
async def test_watcher_advanced_conditions_baseline_and_cooldown_are_enforced(tmp_path):
    watcher_db = WatcherDatabase(tmp_path / "watchers.db")
    monitor = Monitor(
        id="advanced-price", name="Advanced price", type="custom", interval_seconds=20,
        config={
            "baseline": {"price": 100},
            "conditions": [{"type": "threshold", "field": "price", "below": 90}],
            "condition_operator": "AND",
            "alert_policy": {"cooldown_seconds": 600},
            "priority": "high",
        },
    )
    watcher_db.insert_monitor(monitor)
    emitted: list[str] = []

    async def on_event(name, _payload):
        emitted.append(name)

    scheduler = WatcherScheduler(
        watcher_db,
        fetchers={"custom": _SequenceFetcher([FetchResult(True, {"price": 80}), FetchResult(True, {"price": 79})])},
        on_event=on_event,
    )
    try:
        first = await scheduler.check_monitor(monitor, force=True)
        second = await scheduler.check_monitor(watcher_db.get_monitor(monitor.id), force=True)
        assert first is not None and first.change_percent == -20.0 and first.severity == "critical"
        assert second is not None and second.suppressed is True and second.suppression_reason == "cooldown"
        assert second.notified is True
        assert emitted.count("alert.created") == 1
        assert "alert.suppressed" in emitted
        persisted = watcher_db.list_events(monitor.id)
        assert len(persisted) == 2 and any(item.suppressed for item in persisted)
    finally:
        await scheduler.close()
        watcher_db.close()


def test_watcher_structured_inspection_and_false_positive_feedback(tmp_path):
    handlers = WatcherToolHandlers(tmp_path / "watchers.db")
    try:
        created = json.loads(handlers.create({
            "name": "Policy watcher", "url": "https://example.com", "response_format": "structured",
            "conditions": [{"type": "regex", "field": "body", "pattern": "sale"}],
            "alert_policy": {"dedupe_window_seconds": 30},
        }))
        assert set(created) == {
            "ok", "status", "summary", "data", "artifacts", "warnings", "errors",
            "next_actions", "provenance", "metrics", "undo_id",
        }
        watcher_id = created["data"]["watcher"]["id"]
        handlers.db.insert_event(Event(
            id="feedback-event", monitor_id=watcher_id, event_type="content_change",
            old_value="old", new_value="new", change_summary="Sale page changed", confidence=0.9,
        ))
        inspection = json.loads(handlers.get({"watcher_id": watcher_id, "response_format": "structured"}))
        event = inspection["data"]["events"][0]
        assert event["confidence"] == 0.9 and event["change"]["current"] == "new"
        feedback = json.loads(handlers.acknowledge({
            "event_id": "feedback-event", "feedback": "false_positive", "response_format": "structured",
        }))
        assert feedback["ok"] is True
        assert handlers.db.get_event("feedback-event").feedback == "false_positive"
    finally:
        handlers.close()


def test_phone_advanced_surfaces_are_private_and_confirmed(monkeypatch):
    executor = ToolExecutor(_Store(), config=AppConfig(phone=PhoneConfig(enabled=True)))
    secret = "private verification code 445566"
    sent: list[tuple[str, str]] = []
    calls: list[str] = []

    monkeypatch.setattr(
        "ares.tools.executor._kdeconnect_bridge.get_recent_notifications",
        lambda limit=20: json.dumps({"ok": True, "snapshot": True, "notifications": [{"id": "n", "app": "Mail", "text": secret, "unread": True}]}),
    )
    monkeypatch.setattr(
        "ares.tools.executor._kdeconnect_bridge.search_contacts",
        lambda query, limit=20: json.dumps({"ok": True, "limit": limit, "contacts": [{"name": "Asha", "numbers": ["+15555550100"]}]}),
    )
    monkeypatch.setattr("ares.tools.executor._kdeconnect_bridge.status", lambda: {"ok": True, "reachable": True})
    monkeypatch.setattr(
        "ares.tools.executor._kdeconnect_bridge.send_sms",
        lambda number, message: sent.append((number, message)) or json.dumps({"ok": True, "sent": True}),
    )
    monkeypatch.setattr(
        "ares.tools.executor._adb_bridge.phone_status",
        lambda: json.dumps({"ok": True, "capability_matrix": {"calls": True}, "adb": {"connected": True, "devices": ["pixel"]}}),
    )
    monkeypatch.setattr(
        "ares.tools.executor._adb_bridge.call_number",
        lambda number, confirm=False: calls.append(number) or json.dumps({"ok": True, "dialed": True, "call_id": "call-1"}),
    )
    try:
        notifications = executor.execute("phone_get_notifications", {"keywords": ["verification"], "response_format": "structured"})
        assert secret not in notifications
        assert json.loads(notifications)["data"]["privacy"]["raw_content_returned"] is False

        contacts = json.loads(executor.execute("phone_search_contact", {"query": "asha", "response_format": "structured"}))
        assert contacts["data"]["contacts"]["candidates"][0]["channels"][0]["value"].endswith("0100")
        assert "+15555550100" not in json.dumps(contacts)

        preview = json.loads(executor.execute("phone_send_sms", {
            "number": "+15555550100", "template": "Hi {{name}}", "variables": {"name": "Asha"},
            "mode": "preview", "response_format": "structured",
        }))
        assert preview["status"] == "preview" and sent == []
        submitted = json.loads(executor.execute("phone_send_sms", {
            "number": "+15555550100", "template": "Hi {{name}}", "variables": {"name": "Asha"},
            "confirm": True, "response_format": "structured",
        }))
        assert submitted["ok"] is True and sent == [("+15555550100", "Hi Asha")]

        preflight = json.loads(executor.execute("phone_call_number", {
            "number": "+15555550100", "mode": "preflight", "response_format": "structured",
        }))
        assert preflight["status"] == "preview" and calls == []
        call = json.loads(executor.execute("phone_call_number", {
            "number": "+15555550100", "confirm": True, "response_format": "structured",
        }))
        assert call["ok"] is True and calls == ["+15555550100"]
    finally:
        executor.close()
