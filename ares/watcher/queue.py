"""Durable event queue facade."""

from ares.watcher.database import WatcherDatabase
from ares.watcher.models import Event


class EventQueue:
    def __init__(self, db: WatcherDatabase) -> None:
        self.db = db

    def add_event(self, event: Event) -> None:
        self.db.insert_event(event)

    def get_unnotified_events(self, limit: int = 100) -> list[Event]:
        return self.db.get_unnotified_events(limit)

    def mark_notified(self, event_id: str) -> None:
        self.db.mark_event_notified(event_id)

    def get_events_for_monitor(self, monitor_id: str, limit: int = 10) -> list[Event]:
        return self.db.list_events(monitor_id, limit=limit)
