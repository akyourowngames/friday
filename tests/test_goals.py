"""Goal Manager storage, evidence, context, export, and tool integration tests."""

from __future__ import annotations

import json
from datetime import date, timedelta

import pytest

from ares.context_blend import build_context_prompt, format_goals
from ares.goals import GoalConflictError, GoalStore, GoalToolHandlers
from ares.memory import MemoryStore
from ares.tasks import TaskStore
from ares.tools import ToolExecutor
from ares.tools.exporter import export_data, import_data


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
    source.create("Verify import hierarchy", parent_goal_id=goal["goal_id"])
    output = export_data(memory_store=memory, goal_store=source, path=tmp_path / "goals.json", profile="goals")
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["version"] == 3
    exported_root = next(item for item in payload["goals"] if item["title"] == "Build durable goals")
    assert exported_root["links"]["actions"] == ["9"]

    imported_memory = MemoryStore(db_path=tmp_path / "import.db", embedding_provider=fake_embedding_provider)
    imported = GoalStore(tmp_path / "import.db", connection=imported_memory.conn)
    counts = import_data(output, memory_store=imported_memory, goal_store=imported)
    assert counts["goals"] == 2
    restored = imported.search("durable")[0]
    assert restored["progress_percent"] == 50
    assert imported.linked_refs(restored["goal_id"])["actions"] == ["9"]
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
    finally:
        executor.close()
        memory.close()
