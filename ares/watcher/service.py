"""High-level watcher service shared by the dashboard and control surfaces."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, AsyncIterator

from ares.watcher.database import WatcherDatabase
from ares.watcher.fetchers import ToolRunner
from ares.watcher.notifier import NotificationDispatcher
from ares.watcher.scheduler import GoalSignalCallback, WatcherScheduler


class WatcherService:
    def __init__(self, database_path: str | Path, *, notification_settings: dict[str, Any] | None = None,
                 max_concurrency: int = 8, poll_seconds: float = 5.0,
                 goal_signal_handler: GoalSignalCallback | None = None,
                 tool_runner: ToolRunner | None = None, allow_mutating_tools: bool = False,
                 max_tool_steps: int = 8, max_tool_output_chars: int = 2_000_000) -> None:
        self.db = WatcherDatabase(database_path)
        self._subscribers: set[asyncio.Queue] = set()
        self.notifier = NotificationDispatcher(self.db, notification_settings)
        self.scheduler = WatcherScheduler(
            self.db,
            notifier=self.notifier,
            on_event=self.publish,
            goal_signal_handler=goal_signal_handler,
            max_concurrency=max_concurrency,
            tool_runner=tool_runner,
            allow_mutating_tools=allow_mutating_tools,
            max_tool_steps=max_tool_steps,
            max_tool_output_chars=max_tool_output_chars,
        )
        self.poll_seconds = max(.5, float(poll_seconds))
        self._task: asyncio.Task | None = None

    async def start(self, poll_seconds: float | None = None) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self.scheduler.run(poll_seconds or self.poll_seconds), name="ares-watcher-scheduler")

    async def stop(self) -> None:
        self.scheduler.stop()
        if self._task:
            try: await asyncio.wait_for(self._task, 10)
            except asyncio.TimeoutError: self._task.cancel()
        await self.scheduler.close()
        self.db.close()

    async def publish(self, name: str, payload: dict[str, Any]) -> None:
        message = {"type":name,"payload":payload}
        for queue in tuple(self._subscribers):
            if queue.full():
                try: queue.get_nowait()
                except asyncio.QueueEmpty: pass
            queue.put_nowait(message)

    async def subscribe(self) -> AsyncIterator[dict[str, Any]]:
        queue: asyncio.Queue = asyncio.Queue(maxsize=100)
        self._subscribers.add(queue)
        try:
            while True: yield await queue.get()
        finally:
            self._subscribers.discard(queue)
