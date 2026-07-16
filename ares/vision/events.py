"""Small, local primitives for carrying vision events between components.

The vision service deliberately keeps this module independent from FastAPI,
SQLite, and any notification provider.  A caller can subscribe to the bus
while a source is running and decide separately whether an event should be
persisted, shown in the UI, or sent as a notification.
"""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Iterable

from .models import VisualEvent


def utc_now() -> datetime:
    """Return an aware timestamp so event ordering is unambiguous."""

    return datetime.now(timezone.utc)


def value_for(record: object, name: str, default: Any = None) -> Any:
    """Read a field from a Pydantic model, dataclass, or mapping.

    The core only receives Pydantic models in production, but accepting a
    mapping makes the utilities convenient for lightweight adapters and tests.
    """

    if isinstance(record, dict):
        return record.get(name, default)
    return getattr(record, name, default)


def event_data(event: VisualEvent | object) -> dict[str, Any]:
    """Produce a plain representation without coupling callers to Pydantic."""

    if hasattr(event, "model_dump"):
        return event.model_dump(mode="python")  # type: ignore[no-any-return]
    if isinstance(event, dict):
        return dict(event)
    return {
        key: value_for(event, key)
        for key in (
            "event_id",
            "event_type",
            "source_id",
            "occurred_at",
            "subject",
            "description",
            "confidence",
            "previous_state",
            "current_state",
            "frame_reference",
            "remembered",
        )
    }


def make_visual_event(
    *,
    event_type: str,
    source_id: str,
    subject: str | None,
    description: str,
    confidence: float,
    previous_state: dict[str, Any] | None = None,
    current_state: dict[str, Any] | None = None,
    occurred_at: datetime | None = None,
    frame_reference: str | None = None,
    remembered: bool = False,
) -> VisualEvent:
    """Create a typed event with safe, consistent defaults.

    Keeping construction here makes scene differencing and the watch engine
    agree on timestamps and confidence bounds without assigning IDs themselves
    (``VisualEvent`` owns that responsibility).
    """

    return VisualEvent(
        event_type=event_type,
        source_id=source_id,
        occurred_at=occurred_at or utc_now(),
        subject=subject,
        description=description,
        confidence=max(0.0, min(1.0, float(confidence))),
        previous_state=previous_state,
        current_state=current_state,
        frame_reference=frame_reference,
        remembered=remembered,
    )


class PublishCount(int):
    """An integer publish result that can also be awaited.

    ``VisionEventBus.publish`` performs no I/O, so publishing immediately is
    useful for synchronous scene processing.  Making the result awaitable also
    lets async source workers write ``await bus.publish(event)`` naturally.
    """

    def __new__(cls, delivered: int) -> "PublishCount":
        return int.__new__(cls, delivered)

    def __await__(self):  # type: ignore[no-untyped-def]
        async def _completed() -> int:
            return int(self)

        return _completed().__await__()


@dataclass(eq=False, slots=True)
class EventSubscription:
    """A filtered, bounded subscription to a :class:`VisionEventBus`."""

    _bus: "VisionEventBus"
    queue: asyncio.Queue[VisualEvent]
    source_id: str | None = None
    event_types: frozenset[str] | None = None
    dropped_events: int = 0
    closed: bool = False

    def matches(self, event: VisualEvent) -> bool:
        if self.source_id is not None and value_for(event, "source_id") != self.source_id:
            return False
        if self.event_types is not None:
            event_type = _normalise_event_type(value_for(event, "event_type", ""))
            if event_type not in self.event_types:
                return False
        return True

    async def get(self) -> VisualEvent:
        """Wait for the next event, matching :class:`asyncio.Queue`'s API."""

        return await self.queue.get()

    def get_nowait(self) -> VisualEvent:
        return self.queue.get_nowait()

    def empty(self) -> bool:
        return self.queue.empty()

    def qsize(self) -> int:
        return self.queue.qsize()

    def task_done(self) -> None:
        self.queue.task_done()

    async def join(self) -> None:
        await self.queue.join()

    def close(self) -> None:
        self._bus.unsubscribe(self)

    def __aiter__(self) -> AsyncIterator[VisualEvent]:
        return self

    async def __anext__(self) -> VisualEvent:
        if self.closed:
            raise StopAsyncIteration
        return await self.get()


def _normalise_event_type(value: object) -> str:
    raw = getattr(value, "value", value)
    return str(raw or "").strip().lower()


class VisionEventBus:
    """In-memory fan-out bus with lossy, bounded subscriber queues.

    Slow consumers receive the newest events instead of allowing camera/screen
    processing to block or accumulate unbounded memory.  The bus is local to a
    process by design; durable history belongs in the vision store.
    """

    def __init__(
        self,
        *,
        queue_size: int = 100,
        history_size: int = 200,
    ) -> None:
        if queue_size < 1:
            raise ValueError("queue_size must be at least one")
        if history_size < 1:
            raise ValueError("history_size must be at least one")
        self.queue_size = int(queue_size)
        self._subscribers: set[EventSubscription] = set()
        self._history: deque[VisualEvent] = deque(maxlen=int(history_size))

    @property
    def history(self) -> tuple[VisualEvent, ...]:
        """Return a read-only snapshot of the recent in-process event history."""

        return tuple(self._history)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def subscribe(
        self,
        source_id: str | None = None,
        event_types: Iterable[str] | None = None,
        *,
        maxsize: int | None = None,
        queue_size: int | None = None,
        max_queue_size: int | None = None,
    ) -> EventSubscription:
        """Subscribe to every event or a selected source/type subset.

        ``maxsize``, ``queue_size`` and ``max_queue_size`` are accepted as
        compatibility-friendly names for the per-subscriber bound.
        """

        requested_size = maxsize
        if requested_size is None:
            requested_size = queue_size
        if requested_size is None:
            requested_size = max_queue_size
        size = self.queue_size if requested_size is None else int(requested_size)
        if size < 1:
            raise ValueError("subscription queue size must be at least one")
        kinds = (
            frozenset(_normalise_event_type(item) for item in event_types)
            if event_types is not None
            else None
        )
        subscription = EventSubscription(
            _bus=self,
            queue=asyncio.Queue(maxsize=size),
            source_id=source_id,
            event_types=kinds,
        )
        self._subscribers.add(subscription)
        return subscription

    def unsubscribe(self, subscription: EventSubscription | asyncio.Queue[VisualEvent]) -> None:
        """Stop delivering events to a subscription (safe to call repeatedly)."""

        if isinstance(subscription, EventSubscription):
            self._subscribers.discard(subscription)
            subscription.closed = True
            return
        for item in tuple(self._subscribers):
            if item.queue is subscription:
                self._subscribers.discard(item)
                item.closed = True

    def publish(self, event: VisualEvent) -> PublishCount:
        """Fan an event out without ever waiting for a slow subscriber."""

        self._history.append(event)
        delivered = 0
        for subscription in tuple(self._subscribers):
            if subscription.closed or not subscription.matches(event):
                continue
            queue = subscription.queue
            if queue.full():
                try:
                    queue.get_nowait()
                    # The discarded item will never be processed, so settle its
                    # queue task count for callers that choose to use ``join``.
                    queue.task_done()
                except asyncio.QueueEmpty:
                    pass
                except ValueError:
                    # A consumer may have called task_done already; dropping
                    # the event is still the right bounded-queue behaviour.
                    pass
                subscription.dropped_events += 1
            try:
                queue.put_nowait(event)
                delivered += 1
            except asyncio.QueueFull:
                # Defensive only: another producer can fill the queue between
                # the check and put when this bus is shared across threads.
                subscription.dropped_events += 1
        return PublishCount(delivered)

    emit = publish

    async def publish_async(self, event: VisualEvent) -> int:
        return int(self.publish(event))

    async def emit_async(self, event: VisualEvent) -> int:
        return int(self.publish(event))

    def publish_many(self, events: Iterable[VisualEvent]) -> int:
        return sum(int(self.publish(event)) for event in events)

    def clear_history(self) -> None:
        self._history.clear()

    def close(self) -> None:
        for subscription in tuple(self._subscribers):
            self.unsubscribe(subscription)


# Short alias for integrations that do not need the longer domain name.
EventBus = VisionEventBus
