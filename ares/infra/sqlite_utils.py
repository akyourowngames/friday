"""SQLite connection helpers shared by Ares stores."""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar


T = TypeVar("T")

_LOCK_ERROR_MARKERS = (
    "database is locked",
    "database schema is locked",
    "database table is locked",
    "database is busy",
)


def is_sqlite_lock_error(exc: BaseException) -> bool:
    """Return whether SQLite rejected work because another writer owns the DB."""
    return isinstance(exc, sqlite3.OperationalError) and any(
        marker in str(exc).lower() for marker in _LOCK_ERROR_MARKERS
    )


def retry_sqlite_locked(
    operation: Callable[[], T],
    *,
    description: str = "SQLite operation",
    attempts: int = 8,
    initial_delay: float = 0.15,
    on_retry: Callable[[sqlite3.OperationalError], None] | None = None,
) -> T:
    """Retry a short-lived SQLite writer collision with bounded backoff.

    SQLite's busy timeout handles most collisions, but schema changes can still
    return ``database is locked`` immediately on Windows.  Initialization work
    is idempotent, so retrying it is safer than failing an entire Ares turn.
    """
    delay = max(0.0, initial_delay)
    for attempt in range(max(1, attempts)):
        try:
            return operation()
        except sqlite3.OperationalError as exc:
            if not is_sqlite_lock_error(exc) or attempt == max(1, attempts) - 1:
                raise
            if on_retry is not None:
                on_retry(exc)
            time.sleep(delay)
            delay = min(max(delay * 2, 0.05), 2.0)

    raise RuntimeError(f"{description} retry loop ended unexpectedly")  # pragma: no cover


def connect_sqlite(db_path: Path) -> sqlite3.Connection:
    """Open a SQLite connection tuned for multiple Ares stores."""
    conn = sqlite3.connect(str(db_path), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        conn.execute("PRAGMA journal_mode = WAL")
    except sqlite3.DatabaseError:
        pass
    return conn
