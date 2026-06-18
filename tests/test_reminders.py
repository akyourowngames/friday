"""Tests for reminder service."""

import pytest

from ares.reminders import DesktopNotifier, ReminderService
from ares.tasks import TaskStore


class RecordingNotifier(DesktopNotifier):
    def __init__(self):
        super().__init__(enabled=True)
        self.sent = []

    def notify(self, title: str, message: str) -> bool:
        self.sent.append((title, message))
        return True


@pytest.mark.asyncio
async def test_reminder_service_notifies_and_marks_sent(tmp_path):
    tasks = TaskStore(db_path=tmp_path / "tasks.db")
    task_id = tasks.create("Take medicine", due="2000-01-01T00:00:00+00:00")
    seen = []
    notifier = RecordingNotifier()
    service = ReminderService(tasks, seen.append, notifier=notifier)

    count = await service.run_once()

    assert count == 1
    assert seen[0]["id"] == task_id
    assert notifier.sent[0][0] == "Reminder: Take medicine"
    assert tasks.get(task_id)["reminder_sent_at"] is not None
    assert await service.run_once() == 0
