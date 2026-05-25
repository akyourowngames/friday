from __future__ import annotations

import threading
from pathlib import Path

from .bus import EventBus
from .configuration import WatcherConfig
from .ingest import IngestPipeline


class WatchdogUnavailable(RuntimeError):
    pass


class DebouncedWatcher:
    def __init__(self, config: WatcherConfig, pipeline: IngestPipeline, event_bus: EventBus | None = None):
        self.config = config
        self.pipeline = pipeline
        self.event_bus = event_bus
        self._observer = None
        self._timers: dict[str, threading.Timer] = {}
        self._lock = threading.Lock()

    def start(self):
        try:
            from watchdog.events import FileSystemEventHandler
            from watchdog.observers import Observer
        except ImportError as exc:
            raise WatchdogUnavailable("watchdog is required for live OS-backed folder watching. Install requirements.txt first.") from exc

        watcher = self

        class Handler(FileSystemEventHandler):
            def on_created(self, event):
                watcher._handle(event.src_path, "DIR_CREATED" if event.is_directory else "FILE_CREATED")

            def on_modified(self, event):
                if not event.is_directory:
                    watcher._handle(event.src_path, "FILE_MODIFIED")

            def on_deleted(self, event):
                payload = watcher.pipeline.delete_path(event.src_path)
                watcher._publish(payload)

            def on_moved(self, event):
                payload = watcher.pipeline.move_path(event.src_path, event.dest_path)
                watcher._publish(payload)

        observer = Observer()
        observer.schedule(Handler(), str(self.config.watch_path), recursive=True)
        observer.start()
        self._observer = observer
        return observer

    def stop(self):
        with self._lock:
            for timer in self._timers.values():
                timer.cancel()
            self._timers.clear()
        if self._observer is not None:
            self._observer.stop()
            self._observer.join(timeout=5)
            self._observer = None

    def _handle(self, path: str, event_type: str):
        resolved = str(Path(path).expanduser().resolve())
        delay = max(0, self.config.debounce_ms) / 1000
        with self._lock:
            old = self._timers.pop(resolved, None)
            if old is not None:
                old.cancel()
            timer = threading.Timer(delay, self._process, args=(resolved, event_type))
            self._timers[resolved] = timer
            timer.daemon = True
            timer.start()

    def _process(self, path: str, event_type: str):
        with self._lock:
            self._timers.pop(path, None)
        result = self.pipeline.ingest_path(path, event_type)
        if result is not None:
            self._publish(result.get("event"))

    def _publish(self, event: dict | None):
        if event is not None and self.event_bus is not None:
            self.event_bus.publish(event)
