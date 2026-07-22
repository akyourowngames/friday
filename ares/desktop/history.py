"""In-memory store for recent voice exchanges shown in the tray menu."""

from __future__ import annotations

from collections import deque
from typing import Any


class HistoryStore:
    """Fixed-size ring buffer of recent user/assistant exchanges."""

    def __init__(self, max_size: int = 5) -> None:
        self._max_size = max(1, max_size)
        self._entries: deque[dict[str, str]] = deque(maxlen=self._max_size)

    def add(self, user_text: str, assistant_text: str) -> None:
        self._entries.append({"user": user_text, "assistant": assistant_text})

    def recent(self, limit: int | None = None) -> list[dict[str, str]]:
        entries = list(self._entries)
        if limit is not None:
            entries = entries[-limit:]
        return entries

    def clear(self) -> None:
        self._entries.clear()
