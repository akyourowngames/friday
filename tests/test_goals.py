"""Goal Manager storage, evidence, context, export, and tool integration tests."""

from __future__ import annotations

import json
import sqlite3
from datetime import date, timedelta

import pytest

from ares.context_blend import build_context_prompt, format_goals
from ares.goals import GoalConflictError, GoalStore, GoalToolHandlers
from ares.memory import MemoryStore
from ares.tasks import TaskStore
from ares.tools import ToolExecutor
from ares.tools.exporter import export_data, import_data
from ares.watcher.database import WatcherDatabase
from ares.watcher.models import Event, Monitor


def test_goal_lifecycle_revision_search_and_timeline(tmp_path):
    store = GoalStore(tmp_path / "ares.db")
    try:
        goal = store.create(
            "Ship watcher engine",
            description="Deliver a verified MVP",
            category="engineering",
            priority="high",
            target_date=(date.today() + timedelta(days=5)).isoformat(),
        )
        assert goal["status"] == "active"
        assert goal["revision"] == 1
        assert store.search("watcher")[0]["goal_id"] == goal["goal_id"]
        assert store.due_soon(within_days=7)[0]["days_remaining"] == 5

        updated = store.record_progress(goal["goal_id"], note="API is stable", progress_percent=35, expected_revision=1)
        assert updated["progress_percent"] == 35
        assert updated["progress_mode"] == "manual"
        assert updated["revision"] == 2
        assert store.list_events(goal["goal_id"])[0]["note"] == "API is stable"

        with pytest.raises(GoalConflictError):
            store.pause(goal["goal_id"], expected_revision=1)
        completed = store.complete(goal["goal_id"], expected_revision=2)
        assert completed["status"] == "completed"
        assert completed["progress_percent"] == 100
        assert completed["completed_at"]
    finally:
        store.close()


def test_multilevel_decomposition_tree_and_cycle_protection(tmp_path):
    store = GoalStore(tmp_path / "ares.db")
    try:
        root = store.create("Launch Ares")
        children = store.decompose(root["goal_id"], [
            {"title": "Ship the product", "priority": "high"},
            {"title": "Publish the launch story"},
        ])
        grandchild = store.create("Verify release build", parent_goal_id=children[0]["goal_id"])
        tree = store.tree(root["goal_id"])
        assert [child["title"] for child in tree["children"]] == ["Ship the product", "Publish the launch story"]
        assert tree["children"][0]["children"][0]["goal_id"] == grandchild["goal_id"]
        with pytest.raises(GoalConflictError):
            store.update(root["goal_id"], parent_goal_id=grandchild["goal_id"])
    finally:
        store.close()


def test_explicit_progress_sync_uses_children_tasks_and_actions(tmp_path):
    tasks = TaskStore(tmp_path)
    store = GoalStore(tmp_path / "ares.db", task_store=tasks)
    try:
        parent = store.create("Release v1")
        children = store.decompose(parent["goal_id"], [{"title": "Docs"}, {"title": "Build"}])
        store.complete(children[0]["goal_id"])

        task = tasks.create_task("Run verification", [{"step_id": "verify", "tool_name": "get_current_datetime", "arguments": {}}])
        claimed = tasks.claim_task(task["task_id"])
        tasks.record_step_complete(task["task_id"], claimed["lease_id"], step_index=0, summary="Verified")
        tasks.complete_task(task["task_id"], claimed["lease_id"], result_summary="Done")
        store.link(parent["goal_id"], link_type="task", ref_id=task["task_id"])
        store.link(parent["goal_id"], link_type="action", ref_id="42")

        synced = store.recalculate_progress(parent["goal_id"])
        # Docs child, completed task, and action are complete; Build child is not.
        assert synced["progress_percent"] == 75
        assert synced["progress_mode"] == "derived"
        event = store.list_events(parent["goal_id"])[0]
        assert event["event_type"] == "progress_synced"
        assert event["metadata"]["total_evidence"] == 4
    finally:
        store.close()


def test_goal_handlers_require_confirmation_for_delete_and_validate_task_link(tmp_path):
    tasks = TaskStore(tmp_path)
    store = GoalStore(tmp_path / "ares.db", task_store=tasks)
    handlers = GoalToolHandlers(store, tasks)
    try:
        created = json.loads(handlers.create_goal({"title": "Professional launch", "priority": "high"}))
        goal_id = created["goal"]["goal_id"]
        missing = json.loads(handlers.link_goal_task({"goal_id": goal_id, "task_id": "missing"}))
        assert missing["ok"] is False
        confirmation = json.loads(handlers.delete_goal({"goal_id": goal_id}))
        assert confirmation["confirm_required"] is True
        deleted = json.loads(handlers.delete_goal({"goal_id": goal_id, "confirm": True}))
        assert deleted == {"ok": True, "action": "deleted"}
    finally:
        store.close()


def test_goal_context_formats_due_and_overdue_state():
    goals = [{
        "goal_id": 7, "title": "Ship goal manager", "status": "active", "priority": "high",
        "target_date": "2026-07-20", "progress_percent": 40, "progress_mode": "derived",
    }]
    due = [{**goals[0], "days_remaining": 3}]
    overdue = [{**goals[0], "goal_id": 8, "title": "Write docs", "days_remaining": -2}]
    formatted = format_goals(goals, due, overdue)
    assert "#7 [active, high] Ship goal manager" in formatted
    assert "40% progress (derived)" in formatted
    assert "Due soon" in formatted and "Overdue" in formatted
    blended = build_context_prompt(memories=[{"fact_id": 1, "fact_text": "Uses Ares"}], goals=goals, goals_due_soon=due)
    assert blended.index("What I know") < blended.index("## Goals")


def test_goals_export_and_import_with_links(tmp_path, fake_embedding_provider):
    memory = MemoryStore(db_path=tmp_path / "source.db", embedding_provider=fake_embedding_provider)
    source = GoalStore(tmp_path / "source.db", connection=memory.conn)
    goal = source.create("Build durable goals")
    source.record_progress(goal["goal_id"], note="Schema complete", progress_percent=50)
    source.link(goal["goal_id"], link_type="action", ref_id="9")
    source.link(goal["goal_id"], link_type="watcher", ref_id="release-watch")
    source.record_watcher_signal(
        goal["goal_id"], "release-watch", "Release candidate is available",
        source_event_id="release-event", severity="warning",
    )
    source.create("Verify import hierarchy", parent_goal_id=goal["goal_id"])
    output = export_data(memory_store=memory, goal_store=source, path=tmp_path / "goals.json", profile="goals")
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["version"] == 4
    exported_root = next(item for item in payload["goals"] if item["title"] == "Build durable goals")
    assert exported_root["links"]["actions"] == ["9"]
    assert exported_root["links"]["watchers"] == ["release-watch"]
    assert exported_root["watcher_signals"][0]["source_event_id"] == "release-event"

    imported_memory = MemoryStore(db_path=tmp_path / "import.db", embedding_provider=fake_embedding_provider)
    imported = GoalStore(tmp_path / "import.db", connection=imported_memory.conn)
    counts = import_data(output, memory_store=imported_memory, goal_store=imported)
    assert counts["goals"] == 2
    restored = imported.search("durable")[0]
    assert restored["progress_percent"] == 50
    assert imported.linked_refs(restored["goal_id"])["actions"] == ["9"]
    assert imported.linked_refs(restored["goal_id"])["watchers"] == ["release-watch"]
    assert imported.list_watcher_signals(restored["goal_id"])[0]["event_summary"] == "Release candidate is available"
    assert imported.tree(restored["goal_id"])["children"][0]["title"] == "Verify import hierarchy"


def test_goal_tools_integrate_with_executor_and_action_ledger(tmp_path, fake_embedding_provider):
    memory = MemoryStore(db_path=tmp_path / "ares.db", embedding_provider=fake_embedding_provider)
    executor = ToolExecutor(memory)
    try:
        created = json.loads(executor.execute("create_goal", {"title": "Ship the goal system", "priority": "high"}))
        assert created["ok"] is True
        goal_id = created["goal"]["goal_id"]
        progress = json.loads(executor.execute("record_goal_progress", {"goal_id": goal_id, "note": "Store wired", "progress_percent": 20}))
        assert progress["goal"]["progress_percent"] == 20
        actions = executor.action_ledger.search("Ship the goal system")
        assert any(action["action_type"] == "goal_created" for action in actions)
        created_action_id = next(action["action_id"] for action in actions if action["action_type"] == "goal_created")
        linked = json.loads(executor.execute("link_goal_action", {"goal_id": goal_id, "action_id": created_action_id}))
        assert linked["links"]["actions"] == [str(created_action_id)]
        watcher = json.loads(executor.execute("create_watcher", {
            "name": "Release page", "type": "website", "url": "https://example.com/release",
        }))["watcher"]
        watcher_link = json.loads(executor.execute("link_goal_watcher", {"goal_id": goal_id, "watcher_id": watcher["id"]}))
        assert watcher_link["links"]["watchers"] == [watcher["id"]]
        watcher_unlink = json.loads(executor.execute("unlink_goal_watcher", {"goal_id": goal_id, "watcher_id": watcher["id"]}))
        assert watcher_unlink["links"]["watchers"] == []
    finally:
        executor.close()
        memory.close()


def test_watcher_signal_lifecycle_is_idempotent_non_mutating_and_anti_nagged(tmp_path):
    store = GoalStore(tmp_path / "ares.db")
    try:
        goal = store.create("Buy a laptop under $1,000", priority="high")
        store.link(goal["goal_id"], link_type="watcher", ref_id="price-watch")
        first = store.record_watcher_signal(
            goal["goal_id"], "price-watch", "Price dropped to $899 (was $1,099)",
            source_event_id="event-1", event_type="price_change", old_value="1099",
            new_value="899", severity="critical", metadata={"watcher_name": "Laptop price"},
        )
        duplicate = store.record_watcher_signal(
            goal["goal_id"], "price-watch", "Price dropped to $899 (was $1,099)",
            source_event_id="event-1", severity="critical",
        )
        assert first["created"] is True
        assert duplicate["created"] is False
        assert len(store.list_watcher_signals(goal["goal_id"])) == 1
        assert store.get(goal["goal_id"])["status"] == "active"
        assert store.get(goal["goal_id"])["progress_percent"] == 0
        assert store.linked_refs(goal["goal_id"])["watchers"] == ["price-watch"]

        # Context surfaces exactly three turns, then explicit inspection still sees it.
        for _ in range(3):
            enriched = store.contextualize_goals([store.get(goal["goal_id"])])
            assert enriched[0]["watcher_signals"][0]["signal_id"] == first["signal_id"]
        assert store.contextualize_goals([store.get(goal["goal_id"])])[0]["watcher_signals"] == []
        assert store.list_watcher_signals(goal["goal_id"])[0]["surfaced_count"] == 3

        snoozed = store.snooze_watcher_signal(first["signal_id"], hours=48, note="Revisit Friday")
        assert snoozed["snoozed_until"]
        assert store.pending_watcher_signals(goal["goal_id"]) == []
        acknowledged = store.acknowledge_watcher_signal(first["signal_id"], resolution="dismissed")
        assert acknowledged["acknowledged"] is True
        assert store.list_watcher_signals(goal["goal_id"]) == []
        assert store.list_watcher_signals(goal["goal_id"], include_acknowledged=True)[0]["resolution"] == "dismissed"
    finally:
        store.close()


def test_goal_mutation_atomically_resolves_its_own_watcher_signal(tmp_path):
    store = GoalStore(tmp_path / "ares.db")
    try:
        goal = store.create("Buy the laptop")
        other = store.create("Unrelated goal")
        signal = store.record_watcher_signal(
            goal["goal_id"], "price-watch", "Price reached target", source_event_id="event-2",
        )
        wrong = store.record_watcher_signal(
            other["goal_id"], "other-watch", "Other change", source_event_id="event-3",
        )
        before = store.get(goal["goal_id"])
        with pytest.raises(ValueError, match="does not belong"):
            store.update(goal["goal_id"], progress_percent=50, resolves_signal_id=wrong["signal_id"])
        assert store.get(goal["goal_id"])["revision"] == before["revision"]

        completed = store.complete(goal["goal_id"], resolves_signal_id=signal["signal_id"])
        resolved = store.get_watcher_signal(signal["signal_id"])
        assert completed["status"] == "completed"
        assert resolved["acknowledged"] is True
        assert resolved["resolution"] == "goal_completed"
        assert any(event["event_type"] == "watcher_signal_acknowledged" for event in store.list_events(goal["goal_id"]))
    finally:
        store.close()


def test_goal_context_includes_watcher_signal_and_confirmation_boundary():
    goal = {
        "goal_id": 12, "title": "Buy a laptop", "status": "active", "priority": "high",
        "target_date": "2026-07-25", "progress_percent": 40, "progress_mode": "manual",
        "watcher_signals": [{
            "signal_id": 9, "watcher_id": "price", "severity": "critical",
            "event_summary": "Price dropped to $899", "created_at": "2026-07-13T08:00:00Z",
            "metadata": {"watcher_name": "Laptop price"},
        }],
    }
    formatted = format_goals([goal])
    assert "New watcher signal #9 [CRITICAL] from Laptop price" in formatted
    assert "ask before updating/completing" in formatted


def test_signal_acknowledgement_reconciles_source_event_after_all_goals(tmp_path):
    store = GoalStore(tmp_path / "ares.db")
    watcher_db = WatcherDatabase(tmp_path / "watchers.db")
    try:
        watcher_db.insert_monitor(Monitor(id="shared-watch", name="Shared", type="website", url="https://example.com"))
        watcher_db.insert_event(Event(id="source-event", monitor_id="shared-watch", event_type="content_change"))
        goals = [store.create("Goal one"), store.create("Goal two")]
        signals = [
            store.record_watcher_signal(
                goal["goal_id"], "shared-watch", "Shared change", source_event_id="source-event",
            )
            for goal in goals
        ]
        handlers = GoalToolHandlers(store, watcher_db_provider=lambda: watcher_db)
        first = json.loads(handlers.acknowledge_goal_signal({"signal_id": signals[0]["signal_id"]}))
        assert first["source_watcher_event_acknowledged"] is False
        assert watcher_db.get_event("source-event").acknowledged is False
        second = json.loads(handlers.acknowledge_goal_signal({"signal_id": signals[1]["signal_id"]}))
        assert second["source_watcher_event_acknowledged"] is True
        assert watcher_db.get_event("source-event").acknowledged is True
    finally:
        watcher_db.close()
        store.close()


def test_goal_signal_schema_migrates_early_plan_table_without_data_loss(tmp_path):
    path = tmp_path / "ares.db"
    conn = sqlite3.connect(path)
    conn.execute(
        """CREATE TABLE goal_watcher_signals (
           signal_id INTEGER PRIMARY KEY AUTOINCREMENT, goal_id INTEGER NOT NULL,
           watcher_id TEXT NOT NULL, event_summary TEXT NOT NULL, created_at TEXT NOT NULL,
           acknowledged INTEGER NOT NULL DEFAULT 0)"""
    )
    conn.execute(
        "INSERT INTO goal_watcher_signals(goal_id, watcher_id, event_summary, created_at) VALUES (1, 'legacy', 'Old signal', '2026-07-13T00:00:00Z')"
    )
    conn.commit()
    conn.close()

    store = GoalStore(path)
    try:
        columns = {row["name"] for row in store.conn.execute("PRAGMA table_info(goal_watcher_signals)")}
        assert {"source_event_id", "severity", "surfaced_count", "snoozed_until", "metadata_json"} <= columns
        restored = store.list_watcher_signals(include_acknowledged=True)[0]
        assert restored["event_summary"] == "Old signal"
        assert restored["surfaced_count"] == 0
    finally:
        store.close()
