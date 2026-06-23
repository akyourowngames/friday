"""Tests for the task management system."""

from pathlib import Path

import pytest

from ares.tools.tasks import TaskStore


@pytest.fixture
def store(tmp_path):
    """Create a fresh TaskStore with a temp database."""
    db_path = tmp_path / "test_tasks.db"
    return TaskStore(db_path=db_path)


class TestTaskStore:
    def test_create_task(self, store):
        """Creating a task returns an ID and stores it."""
        task_id = store.create("Call the dentist", due="2026-06-19T14:00:00", priority="medium")
        assert task_id is not None
        assert task_id > 0
        task = store.get(task_id)
        assert task["due"].startswith("2026-06-19T14:00:00")
        assert task["reminder_at"] == task["due"]

    def test_get_task(self, store):
        """Getting a task by ID returns the correct task."""
        task_id = store.create("Buy groceries")
        task = store.get(task_id)
        assert task is not None
        assert task["title"] == "Buy groceries"
        assert task["status"] == "pending"

    def test_list_pending(self, store):
        """list_pending returns only pending tasks."""
        store.create("Task one")
        store.create("Task two")
        tid3 = store.create("Task three")
        store.complete(tid3)

        pending = store.list_pending()
        assert len(pending) == 2

    def test_complete_task(self, store):
        """Completing a task changes its status to done."""
        task_id = store.create("Finish report")
        assert store.complete(task_id) is True
        task = store.get(task_id)
        assert task["status"] == "done"
        assert task["completed_at"] is not None

    def test_complete_nonexistent(self, store):
        """Completing a nonexistent task returns False."""
        assert store.complete(99999) is False

    def test_cancel_task(self, store):
        """Cancelling a task changes its status to cancelled."""
        task_id = store.create("Old task")
        assert store.cancel(task_id) is True
        task = store.get(task_id)
        assert task["status"] == "cancelled"

    def test_delete_task(self, store):
        """Deleting a task removes it."""
        task_id = store.create("Temporary task")
        assert store.delete(task_id) is True
        assert store.get(task_id) is None

    def test_create_with_description(self, store):
        """Task stores description when provided."""
        task_id = store.create("Important task", description="Details here")
        task = store.get(task_id)
        assert task["description"] == "Details here"

    def test_get_nonexistent(self, store):
        """Getting a nonexistent task returns None."""
        assert store.get(99999) is None

    def test_natural_due_date_is_normalized(self, store):
        """Natural-language dates are normalized to ISO-style datetimes."""
        task_id = store.create("Natural date", due="tomorrow at 2pm")
        task = store.get(task_id)
        assert task["due"] != "tomorrow at 2pm"
        assert "T" in task["due"]
        assert task["original_due_text"] == "tomorrow at 2pm"

    def test_reminders_fire_once(self, store):
        """Due reminders are returned once and marked as sent."""
        task_id = store.create("Past reminder", due="2000-01-01T00:00:00+00:00")
        due = store.get_due_reminders(now="2000-01-02T00:00:00+00:00")
        assert [task["id"] for task in due] == [task_id]
        assert store.mark_reminded(task_id, reminded_at="2000-01-02T00:00:00+00:00")
        assert store.get_due_reminders(now="2000-01-03T00:00:00+00:00") == []

    def test_search_tasks(self, store):
        """Task search matches titles and descriptions."""
        store.create("Write report", description="Quarterly planning")
        results = store.search("planning")
        assert len(results) == 1
        assert results[0]["title"] == "Write report"

    def test_import_tasks_skips_duplicates(self, store):
        """Task import avoids duplicate title/due/status rows."""
        task_id = store.create("Existing", due="2026-06-19T14:00:00+00:00")
        existing = store.get(task_id)
        count = store.import_tasks([
            existing,
            {"title": "Imported", "due": "2026-06-20T14:00:00+00:00"},
        ])
        assert count == 1
        assert len(store.list_all(include_done=True)) == 2


# ── v2 schema migration tests ────────────────────────────────────


class TestMigrateV2:
    """Tests for the v2 schema migration."""

    def test_migrate_adds_state_column(self, store):
        rows = store.conn.execute("PRAGMA table_info(tasks)").fetchall()
        columns = {row["name"] for row in rows}
        assert "state" in columns

    def test_migrate_adds_plan_column(self, store):
        rows = store.conn.execute("PRAGMA table_info(tasks)").fetchall()
        columns = {row["name"] for row in rows}
        assert "plan" in columns

    def test_migrate_adds_step_columns(self, store):
        rows = store.conn.execute("PRAGMA table_info(tasks)").fetchall()
        columns = {row["name"] for row in rows}
        assert "current_step" in columns
        assert "total_steps" in columns
        assert "completed_steps" in columns

    def test_migrate_adds_retry_columns(self, store):
        rows = store.conn.execute("PRAGMA table_info(tasks)").fetchall()
        columns = {row["name"] for row in rows}
        assert "attempt" in columns
        assert "max_attempts" in columns
        assert "retry_reason" in columns

    def test_migrate_adds_completion_report(self, store):
        rows = store.conn.execute("PRAGMA table_info(tasks)").fetchall()
        columns = {row["name"] for row in rows}
        assert "completion_report" in columns

    def test_migrate_creates_task_events_table(self, store):
        tables = {row[0] for row in store.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        assert "task_events" in tables

    def test_migrate_creates_task_artifacts_table(self, store):
        tables = {row[0] for row in store.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        assert "task_artifacts" in tables

    def test_migrate_maps_done_to_completed(self, store):
        store.conn.execute(
            "INSERT INTO tasks (title, status) VALUES (?, ?)",
            ("old task", "done"),
        )
        store.conn.commit()
        store._migrate_v2()
        row = store.conn.execute("SELECT state FROM tasks WHERE title = 'old task'").fetchone()
        assert row["state"] == "completed"

    def test_migrate_maps_partial_to_failed(self, store):
        store.conn.execute(
            "INSERT INTO tasks (title, status) VALUES (?, ?)",
            ("partial task", "partial"),
        )
        store.conn.commit()
        store._migrate_v2()
        row = store.conn.execute("SELECT state FROM tasks WHERE title = 'partial task'").fetchone()
        assert row["state"] == "failed"

    def test_migrate_is_idempotent(self, store):
        store._migrate_v2()
        store._migrate_v2()  # Should not raise


# ── v2 TaskStore query method tests ──────────────────────────────


class TestTaskStoreV2Methods:
    """Tests for the new v2 TaskStore methods."""

    def test_add_event_returns_id(self, store):
        task_id = store.create("test task")
        event_id = store.add_event(task_id, level="info", step=None, message="test event")
        assert isinstance(event_id, int)
        assert event_id > 0

    def test_get_events_returns_ordered(self, store):
        task_id = store.create("test task")
        store.add_event(task_id, level="info", step=None, message="first")
        store.add_event(task_id, level="success", step=1, message="second")
        store.add_event(task_id, level="error", step=2, message="third")
        events = store.get_events(task_id)
        assert len(events) == 3
        assert events[0]["message"] == "first"
        assert events[2]["message"] == "third"

    def test_get_events_filters_by_task(self, store):
        id1 = store.create("task 1")
        id2 = store.create("task 2")
        store.add_event(id1, level="info", step=None, message="t1 event")
        store.add_event(id2, level="info", step=None, message="t2 event")
        events = store.get_events(id1)
        assert len(events) == 1
        assert events[0]["message"] == "t1 event"

    def test_add_artifact_returns_id(self, store):
        task_id = store.create("test task")
        artifact_id = store.add_artifact(task_id, {
            "step": 1,
            "path": "/tmp/test.md",
            "artifact_type": "write_file",
            "size_bytes": 1024,
            "size_human": "1.0 KB",
            "line_count": 50,
            "description": "Test file",
        })
        assert isinstance(artifact_id, int)
        assert artifact_id > 0

    def test_get_artifacts_returns_all(self, store):
        task_id = store.create("test task")
        store.add_artifact(task_id, {"step": 1, "path": "a.md", "artifact_type": "write_file", "size_bytes": 10, "size_human": "10 B", "line_count": None, "description": None})
        store.add_artifact(task_id, {"step": 2, "path": "b.py", "artifact_type": "edit_file", "size_bytes": 20, "size_human": "20 B", "line_count": None, "description": None})
        artifacts = store.get_artifacts(task_id)
        assert len(artifacts) == 2
        assert artifacts[0]["path"] == "a.md"
        assert artifacts[1]["path"] == "b.py"

    def test_get_tasks_by_state_filters(self, store):
        id1 = store.create("running task")
        id2 = store.create("done task")
        store.update(id1, state="running")
        store.update(id2, state="completed")
        running = store.get_tasks_by_state("running")
        assert len(running) == 1
        assert running[0]["id"] == id1

    def test_set_state_updates_task(self, store):
        task_id = store.create("test task")
        store.set_state(task_id, "running")
        task = store.get(task_id)
        assert task["state"] == "running"


# ── v2: TaskState model tests ────────────────────────────────────


class TestTaskStateModel:
    def test_task_state_enum_has_all_states(self):
        from ares.models import TaskState
        states = {s.value for s in TaskState}
        assert "queued" in states
        assert "planning" in states
        assert "running" in states
        assert "retrying" in states
        assert "completed" in states
        assert "failed" in states
        assert "cancelled" in states

    def test_task_transitions_defined(self):
        from ares.models import TASK_TRANSITIONS
        assert "queued" in TASK_TRANSITIONS
        assert "planning" in TASK_TRANSITIONS["queued"]
        assert "completed" in TASK_TRANSITIONS["running"]
        assert TASK_TRANSITIONS["completed"] == []
