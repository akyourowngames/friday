"""Tests for reminder service."""

from ares.reminders import DesktopNotifier


class RecordingNotifier(DesktopNotifier):
    def __init__(self):
        super().__init__(enabled=True)
        self.sent = []

    def notify(self, title: str, message: str) -> bool:
        self.sent.append((title, message))
        return True
