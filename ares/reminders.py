"""Runtime reminder checking and notification helpers."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


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
