"""Runtime reminder checking and notification helpers."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable
from typing import Any

from ares.tools.tasks import TaskStore

ReminderCallback = Callable[[dict], Any]


class DesktopNotifier:
    """Optional cross-platform desktop notification wrapper."""

    def __init__(self, enabled: bool = True):
        self.enabled = enabled

    def notify(self, title: str, message: str) -> bool:
        """Send a desktop notification if Plyer is available."""
        if not self.enabled:
            return False
        try:
            from plyer import notification

            notification.notify(title=title, message=message, app_name="Ares", timeout=10)
            return True
        except Exception:
            return False


class ReminderService:
    """Polls pending reminders and marks each notification once sent."""

    def __init__(
        self,
        task_store: TaskStore,
        callback: ReminderCallback | None = None,
        *,
        poll_seconds: int = 30,
        notifier: DesktopNotifier | None = None,
    ):
        self.task_store = task_store
        self.callback = callback
        self.poll_seconds = max(1, poll_seconds)
        self.notifier = notifier or DesktopNotifier(enabled=False)

    async def run_once(self) -> int:
        """Check and notify all currently due reminders once."""
        reminders = self.task_store.get_due_reminders()
        for task in reminders:
            title = f"Reminder: {task['title']}"
            due = f" Due: {task['due']}" if task.get("due") else ""
            message = (task.get("description") or task["title"]) + due

            if self.callback is not None:
                result = self.callback(task)
                if inspect.isawaitable(result):
                    await result
            self.notifier.notify(title, message)
            self.task_store.mark_reminded(task["id"])
        return len(reminders)

    async def run(self) -> None:
        """Run the reminder loop until cancelled."""
        while True:
            await self.run_once()
            await asyncio.sleep(self.poll_seconds)
