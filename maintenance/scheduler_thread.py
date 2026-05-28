from __future__ import annotations

import threading
from datetime import datetime, timedelta
from typing import Callable


class DailyScheduler:
    """Fires a callback once per day at or after a configured local cutoff time.

    Idempotent: never fires twice in the same local day. Quiet: any callback
    error is captured to the most recent error string and not raised. Cheap:
    sleeps in 30s ticks so it can be stopped quickly.
    """

    def __init__(
        self,
        callback: Callable[[str], None],
        cutoff_time: str = "03:30",
        check_interval_seconds: int = 30,
        clock: Callable[[], datetime] | None = None,
    ):
        self._callback = callback
        self._cutoff_hour, self._cutoff_minute = _parse_cutoff(cutoff_time)
        self._check_interval = max(5, int(check_interval_seconds))
        self._clock = clock or datetime.now
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_fired_date: str | None = None
        self._last_error: str | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="king-daily-maintenance")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=5)
        self._thread = None

    def status(self) -> dict:
        return {
            "cutoff_time": f"{self._cutoff_hour:02d}:{self._cutoff_minute:02d}",
            "check_interval_seconds": self._check_interval,
            "last_fired_date": self._last_fired_date,
            "last_error": self._last_error,
            "running": bool(self._thread and self._thread.is_alive()),
        }

    def _loop(self) -> None:
        while not self._stop.is_set():
            now = self._clock()
            cutoff = now.replace(hour=self._cutoff_hour, minute=self._cutoff_minute, second=0, microsecond=0)
            today_iso = now.date().isoformat()
            if now >= cutoff and self._last_fired_date != today_iso:
                try:
                    self._callback("scheduler_thread")
                    self._last_error = None
                except Exception as exc:  # noqa: BLE001
                    self._last_error = f"{type(exc).__name__}: {exc}"
                self._last_fired_date = today_iso
            self._stop.wait(self._check_interval)


def _parse_cutoff(value: str) -> tuple[int, int]:
    text = str(value or "03:30").strip()
    parts = text.split(":")
    if len(parts) != 2:
        return 3, 30
    try:
        hour = max(0, min(23, int(parts[0])))
        minute = max(0, min(59, int(parts[1])))
        return hour, minute
    except ValueError:
        return 3, 30


def _next_cutoff(now: datetime, hour: int, minute: int) -> datetime:
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target = target + timedelta(days=1)
    return target
