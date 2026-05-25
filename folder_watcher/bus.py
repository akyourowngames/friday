from __future__ import annotations

import asyncio
from collections.abc import Callable
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass


@dataclass(frozen=True)
class _Subscriber:
    loop: asyncio.AbstractEventLoop
    queue: asyncio.Queue


class EventBus:
    def __init__(self):
        self._subscribers: set[_Subscriber] = set()
        self._listeners: list[Callable[[dict], None]] = []

    def add_listener(self, listener: Callable[[dict], None]):
        self._listeners.append(listener)

    @contextmanager
    def subscribe(self) -> Iterator[asyncio.Queue]:
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()
        subscriber = _Subscriber(loop=loop, queue=queue)
        self._subscribers.add(subscriber)
        try:
            yield queue
        finally:
            self._subscribers.discard(subscriber)

    def publish(self, event: dict):
        for listener in list(self._listeners):
            try:
                listener(event)
            except Exception:
                pass
        for subscriber in list(self._subscribers):
            subscriber.loop.call_soon_threadsafe(_safe_put, subscriber.queue, event)


def _safe_put(queue: asyncio.Queue, event: dict):
    try:
        queue.put_nowait(event)
    except asyncio.QueueFull:
        pass
