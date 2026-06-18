"""Tests for the task management system."""

from pathlib import Path

import pytest

from ares.tasks import TaskStore


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
